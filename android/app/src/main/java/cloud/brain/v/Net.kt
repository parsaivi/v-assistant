package cloud.brain.v

import android.content.Context
import okhttp3.OkHttpClient
import org.json.JSONObject
import java.security.KeyStore
import java.security.SecureRandom
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/**
 * The mutual-TLS client, built from three files baked into the APK.
 *
 * This is the other half of `assistant/core/pki.py`. The server refuses to
 * complete a handshake with anything its private CA did not sign; this side
 * refuses to complete one with anything *but* that CA — which is the part that
 * matters for the phone, because it means the app cannot be pointed at another
 * server, cannot be intercepted by a corporate or café proxy, and does not care
 * what is in Android's system trust store. Neither direction relies on a
 * password, a token, or a hostname anyone could spoof.
 *
 * The private key was generated on the laptop and copied into `assets/` at
 * build time. **It has never been transmitted.** That is the point: a pairing
 * flow needs a channel to pair over, and that channel is the hole. Here there
 * is no first message to intercept, because the credential arrived by
 * `adb install`.
 */
object Net {

    /** Where the server is, written by `cli phone enrol` so the address and
     *  the certificate's SANs cannot disagree. */
    data class Server(val host: String, val port: Int, val fallbacks: List<String>,
                      val device: String) {
        fun base(host: String = this.host) = "https://$host:$port"
        /** The tailnet address first, then whatever LAN leases were current at
         *  enrolment. Tried in order on a cold start, because a phone at home
         *  and a phone on mobile data are the same app with different routes. */
        fun candidates(): List<String> = (listOf(host) + fallbacks).distinct()
    }

    @Volatile private var cached: OkHttpClient? = null
    @Volatile private var cachedServer: Server? = null

    fun server(context: Context): Server {
        cachedServer?.let { return it }
        val raw = context.assets.open("server.json").bufferedReader().use { it.readText() }
        val o = JSONObject(raw)
        val fallbacks = o.optJSONArray("fallbacks")?.let { arr ->
            (0 until arr.length()).map { arr.getString(it) }
        } ?: emptyList()
        val s = Server(o.getString("host"), o.getInt("port"), fallbacks,
                       o.optString("device", "phone"))
        cachedServer = s
        return s
    }

    fun client(context: Context): OkHttpClient {
        cached?.let { return it }
        synchronized(this) {
            cached?.let { return it }
            val built = build(context)
            cached = built
            return built
        }
    }

    private fun build(context: Context): OkHttpClient {
        val cf = CertificateFactory.getInstance("X.509")

        val ca = context.assets.open("ca.crt").use {
            cf.generateCertificate(it) as X509Certificate
        }

        // Trust: our CA and nothing else. Not "our CA in addition to the
        // system store" — an app that also trusts the 150 public roots on an
        // Android phone can be pointed at anything that can get one of them to
        // sign for the address, and this app never needs to talk to anything
        // but one laptop. The CA certificate stays in `assets/` because it is
        // public and pinning it at build time is the point of it.
        val trustStore = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null, null)
            setCertificateEntry("v-ca", ca)
        }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
        tmf.init(trustStore)
        val trustManager = tmf.trustManagers.first { it is X509TrustManager } as X509TrustManager

        // Identity: a certificate from private storage and a private key that
        // is a handle to the secure element. Nothing here can be read off the
        // device — see `Keys`.
        val keyManager = Keys.keyManager(context)

        val ssl = SSLContext.getInstance("TLS").apply {
            init(arrayOf(keyManager), tmf.trustManagers, SecureRandom())
        }

        return OkHttpClient.Builder()
            .sslSocketFactory(ssl.socketFactory, trustManager)
            .connectTimeout(8, TimeUnit.SECONDS)
            // Generous, and deliberately so: an escalation to the model takes
            // ~20s and a Whisper transcription of a long memo can take longer.
            // A phone that gives up at 30s would report "V is down" for the
            // answers that took the most work to produce.
            .readTimeout(5, TimeUnit.MINUTES)
            .writeTimeout(2, TimeUnit.MINUTES)
            // The SSE stream must not be reaped as idle; it is silent for
            // twenty seconds at a time by design.
            .pingInterval(25, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()
    }

    /** Drop the cached client so the next call rebuilds it — used the moment a
     *  certificate is imported, since the client built before that had no
     *  identity to present. */
    fun reset() {
        synchronized(this) { cached = null }
    }
}
