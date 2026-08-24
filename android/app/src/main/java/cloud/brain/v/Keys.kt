package cloud.brain.v

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.security.keystore.StrongBoxUnavailableException
import android.util.Base64
import java.io.File
import java.net.Socket
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.Principal
import java.security.PrivateKey
import java.security.PublicKey
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec
import javax.net.ssl.SSLEngine
import javax.net.ssl.X509ExtendedKeyManager

/**
 * The device identity, held where it cannot be copied.
 *
 * The first version of this app compiled a private key into `assets/`. It was
 * chosen so the key would never cross a network — which was true, and still
 * missed the point: **an APK is world-readable on Android.** `base.apk` is
 * mode 644 inside a world-executable directory, so any installed app that can
 * resolve the path could read the key straight out of the package and become
 * this phone as far as V is concerned. No root, no exploit, one app.
 *
 * So the keypair is generated **here**, inside the Android Keystore, and the
 * private half is never returned to anyone. `KeyStore.getKey()` hands back a
 * handle, not key material; the signing happens in the TEE — or in StrongBox,
 * a separate secure chip, where the hardware is available. There is no API
 * that exports it. Not to this app, not to another app, not to root, not to
 * someone holding the unlocked handset.
 *
 * What leaves the phone is the public key, which is public. The laptop signs a
 * certificate for it (`pki.sign_public_key`) and hands the certificate back.
 * Both files crossing that cable are things you could publish.
 *
 * The certificate lives in `filesDir` rather than in the Keystore entry itself.
 * Replacing the self-signed placeholder Android generates alongside a new key
 * is awkward and version-dependent; a certificate is public anyway, so keeping
 * it in app-private storage and pairing it with the Keystore handle in a
 * custom `X509KeyManager` is both simpler and exactly as safe.
 */
object Keys {

    private const val ALIAS = "v-device"
    private const val STORE = "AndroidKeyStore"
    private const val CERT = "device.crt"

    /** Files the enrolment exchange uses, in the app's external directory —
     *  the one place `adb` can reach on a stock phone. Both are public. */
    private const val PUBKEY_OUT = "device-pubkey.pem"
    private const val CERT_IN = "device-cert.pem"

    class NotEnrolled(message: String) : IllegalStateException(message)

    // ---- the key ---------------------------------------------------------
    private fun store(): KeyStore =
        KeyStore.getInstance(STORE).apply { load(null) }

    /** Generate the keypair once, in the strongest container the phone has. */
    fun ensureKey(): PublicKey {
        val ks = store()
        (ks.getCertificate(ALIAS)?.publicKey)?.let { return it }

        // StrongBox is a physically separate secure element. Where it exists
        // the key is beyond the reach of even a fully compromised kernel; where
        // it doesn't, the TEE is still a different world from the app process.
        // Asking for it and catching the refusal is the only way to find out —
        // `hasSystemFeature` reports the chip, not whether it will accept an
        // EC key of this shape.
        for (strongBox in booleanArrayOf(true, false)) {
            if (strongBox && Build.VERSION.SDK_INT < Build.VERSION_CODES.P) continue
            try {
                return generate(strongBox)
            } catch (e: StrongBoxUnavailableException) {
                continue
            } catch (e: Exception) {
                if (!strongBox) throw e
            }
        }
        throw IllegalStateException("could not create a key in the keystore")
    }

    private fun generate(strongBox: Boolean): PublicKey {
        val spec = KeyGenParameterSpec.Builder(
            ALIAS, KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY)
            .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
            // The digests TLS may ask for. NONE is included because a TLS 1.3
            // ECDSA signature is computed over a hash the stack has already
            // produced, and a key that refuses raw signing fails the handshake
            // with an error that names none of this.
            .setDigests(KeyProperties.DIGEST_NONE, KeyProperties.DIGEST_SHA256,
                        KeyProperties.DIGEST_SHA384, KeyProperties.DIGEST_SHA512)
            .apply {
                if (strongBox && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                    setIsStrongBoxBacked(true)
                }
            }
            .build()
        val gen = KeyPairGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_EC, STORE)
        gen.initialize(spec)
        return gen.generateKeyPair().public
    }

    private fun privateKey(): PrivateKey =
        store().getKey(ALIAS, null) as? PrivateKey
            ?: throw NotEnrolled("no key in the keystore")

    // ---- the certificate --------------------------------------------------
    private fun certFile(context: Context) = File(context.filesDir, CERT)

    fun certificate(context: Context): X509Certificate? {
        val f = certFile(context)
        if (!f.exists()) return null
        return try {
            f.inputStream().use {
                CertificateFactory.getInstance("X.509").generateCertificate(it)
                    as X509Certificate
            }
        } catch (e: Exception) {
            null
        }
    }

    fun isEnrolled(context: Context): Boolean = certificate(context) != null

    /**
     * Run the whole enrolment dance, idempotently. Safe to call on every
     * resume: it generates nothing that exists, publishes nothing that is
     * already published, and imports nothing that has not arrived.
     */
    fun sync(context: Context) {
        try {
            ensureKey()
            importCertificate(context)
            publishPublicKey(context)
        } catch (e: Exception) {
            // Never take the UI down over this. `isEnrolled` stays false and
            // the screen says so, which is the honest state either way.
        }
    }

    /** Write the public key where `cli phone enrol` can fetch it. */
    private fun publishPublicKey(context: Context) {
        val dir = context.getExternalFilesDir(null) ?: return
        val out = File(dir, PUBKEY_OUT)
        // Republished even once enrolled, so re-issuing a certificate later
        // never requires reinstalling the app to get the key back out.
        out.writeText(publicKeyPem())
    }

    fun publicKeyPem(): String {
        val der = ensureKey().encoded          // X.509 SubjectPublicKeyInfo
        val b64 = Base64.encodeToString(der, Base64.NO_WRAP)
        return buildString {
            append("-----BEGIN PUBLIC KEY-----\n")
            b64.chunked(64).forEach { append(it).append('\n') }
            append("-----END PUBLIC KEY-----\n")
        }
    }

    /**
     * Take a certificate the laptop pushed, check it is ours, and move it into
     * private storage.
     *
     * The check matters: a certificate is just a file in a world-readable
     * directory, so anything on the phone can drop one there. One signed for a
     * *different* key would be useless — the handshake would fail, since we
     * cannot produce signatures for it — but it would replace a working
     * certificate and leave V unreachable with no explanation. Comparing the
     * embedded public key against the keystore's costs nothing and turns that
     * into a no-op.
     */
    private fun importCertificate(context: Context) {
        val dir = context.getExternalFilesDir(null) ?: return
        val incoming = File(dir, CERT_IN)
        if (!incoming.exists()) return

        val cert = try {
            incoming.inputStream().use {
                CertificateFactory.getInstance("X.509").generateCertificate(it)
                    as X509Certificate
            }
        } catch (e: Exception) {
            incoming.delete()
            return
        }
        if (cert.publicKey != ensureKey()) {
            incoming.delete()               // not for this key; ignore it
            return
        }
        certFile(context).writeBytes(cert.encoded)
        // Removed once it is in private storage. Leaving a copy in a shared
        // directory serves nothing, and a stale one would be re-imported over
        // a newer certificate on the next resume.
        incoming.delete()
    }

    // ---- TLS --------------------------------------------------------------
    /**
     * The client identity for OkHttp: our certificate, and a private key that
     * exists only as a handle to hardware.
     *
     * Conscrypt signs through the Keystore handle during the handshake, so the
     * key never enters this process's memory even while it is being used.
     */
    fun keyManager(context: Context): X509ExtendedKeyManager {
        val cert = certificate(context)
            ?: throw NotEnrolled("this phone has no certificate yet")
        return object : X509ExtendedKeyManager() {
            private val chain = arrayOf(cert)
            override fun getClientAliases(keyType: String?,
                                          issuers: Array<out Principal>?) =
                arrayOf(ALIAS)
            override fun chooseClientAlias(keyType: Array<out String>?,
                                           issuers: Array<out Principal>?,
                                           socket: Socket?) = ALIAS
            override fun chooseEngineClientAlias(keyType: Array<out String>?,
                                                 issuers: Array<out Principal>?,
                                                 engine: SSLEngine?) = ALIAS
            override fun getServerAliases(keyType: String?,
                                          issuers: Array<out Principal>?) = null
            override fun chooseServerAlias(keyType: String?,
                                           issuers: Array<out Principal>?,
                                           socket: Socket?) = null
            override fun getCertificateChain(alias: String?) = chain
            override fun getPrivateKey(alias: String?) = privateKey()
        }
    }

    /** Where the laptop should look, for the message the app shows when it has
     *  no certificate yet. */
    fun exchangeDir(context: Context): String =
        context.getExternalFilesDir(null)?.absolutePath ?: "(no external storage)"
}
