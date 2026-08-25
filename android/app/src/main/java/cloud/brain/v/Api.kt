package cloud.brain.v

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * The eleven endpoints of `v/core/httpapi.py`, and the turn they return.
 *
 * Most of them are a conversation. `glance` is not — it feeds the home
 * screen — and `attach`/`download` move bytes rather than turns.
 *
 * `Turn` carries every field the design's cost line needs, and carries them as
 * non-optional data. The brief calls the cost line "a product feature, not
 * debug output — dim, never hidden, never behind a flag", so a model where it
 * could be absent would be the wrong model.
 */
data class Turn(
    val text: String,
    val spoken: String,
    val state: String,          // speaking · unsure · refused · degraded
    val route: String,
    val decision: String,
    val citations: List<String>,
    val buttons: List<Button>,
    val cost: Double,
    val allowance: Double,      // what a subscription run consumed, not billed
    val ms: Double,
    val notes: Int,
    val confidence: Double?,
    val model: String,
    val local: Boolean,
    val transcript: String? = null,
    val mine: Boolean = false,  // true for the user's own line in the log
    /** Files this turn produced, plus anything V queued while nobody was
     *  asking. Always present, never null — the log renders it
     *  unconditionally, and a field that is sometimes absent is a crash. */
    val files: List<VFile> = emptyList(),
) {
    /**
     * `◈ route · ⟡ n notes · 1.24s · $0.0000 · ◉ 0.94`
     *
     * A local answer prints an exact `$0.0000` rather than the word "free",
     * because the number *is* the claim: nothing was billed and nothing left
     * the machine. A model run prints `≈$0.0507 allowance`, which is the
     * honest form on a subscription — nothing was billed there either.
     */
    fun costLine(): String {
        val parts = mutableListOf<String>()
        val label = model.ifEmpty { route.substringBefore(':').ifEmpty { "local" } }
        parts += "${V.ROUTE} $label"
        if (notes > 0) parts += "${V.NOTE} $notes notes"
        parts += String.format("%.2fs", ms / 1000.0)
        parts += if (allowance > 0) String.format("≈$%.4f allowance", allowance)
                 else String.format("$%.4f", cost)
        confidence?.let { parts += String.format("${V.CONF} %.2f", it) }
        return parts.joinToString(" · ")
    }

    companion object {
        fun user(text: String) = Turn(
            text = text, spoken = "", state = "speaking", route = "", decision = "",
            citations = emptyList(), buttons = emptyList(), cost = 0.0,
            allowance = 0.0, ms = 0.0, notes = 0, confidence = null,
            model = "", local = true, mine = true)

        fun parse(o: JSONObject): Turn {
            val cites = o.optJSONArray("citations")?.let { a ->
                (0 until a.length()).map { a.getString(it) }
            } ?: emptyList()
            val buttons = o.optJSONArray("buttons")?.let { a ->
                (0 until a.length()).map { i ->
                    val b = a.getJSONObject(i)
                    Button(b.optString("label"), b.optString("action"),
                           b.optJSONObject("payload")?.toString() ?: "{}")
                }
            } ?: emptyList()
            val files = o.optJSONArray("files")?.let { a ->
                (0 until a.length()).map { VFile.parse(a.getJSONObject(it)) }
            } ?: emptyList()
            return Turn(
                files = files,
                text = o.optString("text"),
                spoken = o.optString("spoken", o.optString("text")),
                state = o.optString("state", "speaking"),
                route = o.optString("route"),
                decision = o.optString("decision"),
                citations = cites,
                buttons = buttons,
                cost = o.optDouble("cost", 0.0),
                allowance = o.optDouble("allowance", 0.0),
                ms = o.optDouble("ms", 0.0),
                notes = o.optInt("notes", cites.size),
                confidence = if (o.isNull("confidence")) null
                             else o.optDouble("confidence"),
                model = o.optString("model"),
                local = o.optBoolean("local", true),
                transcript = if (o.has("transcript")) o.optString("transcript") else null)
        }
    }
}

data class Button(val label: String, val action: String, val payload: String)

/**
 * A file sitting in the vault that V has handed over.
 *
 * Metadata only. The bytes stay on the laptop until Parsa asks for them —
 * a turn that answered "here is the chart" by pushing four megabytes down the
 * socket would make every such answer feel like the slow ones, and most of
 * them are never opened.
 */
data class VFile(val rel: String, val name: String, val bytes: Long,
                 val mime: String, val caption: String = "") {

    val isImage: Boolean get() = mime.startsWith("image/")

    /** `1.2 MB` · `840 KB` · `12 B`. Shown on the chip, because tapping it
     *  starts a download and the size is what decides whether to. */
    fun size(): String = when {
        bytes >= 1_000_000 -> String.format("%.1f MB", bytes / 1_000_000.0)
        bytes >= 1_000 -> "${bytes / 1_000} KB"
        else -> "$bytes B"
    }

    companion object {
        fun parse(o: JSONObject) = VFile(
            rel = o.optString("rel"),
            name = o.optString("name"),
            bytes = o.optLong("bytes"),
            mime = o.optString("mime", "application/octet-stream"),
            caption = o.optString("caption"))
    }
}

data class Hello(val notes: Int, val tts: Boolean, val stt: Boolean,
                 val device: String, val seq: Long)

class ApiException(val status: Int, message: String) : IOException(message)

class Api(private val context: Context) {

    private val http get() = Net.client(context)
    private val server get() = Net.server(context)

    /** The address that last worked, so every later call skips the probing. */
    @Volatile private var host: String? = null

    private val json = "application/json; charset=utf-8".toMediaType()

    // ---- the reachable address ------------------------------------------
    /**
     * Try the tailnet address, then the LAN leases recorded at enrolment.
     *
     * A phone moves between networks constantly and there is no discovery
     * protocol here on purpose — mDNS would mean listening on a network this
     * app has no other reason to touch. Trying a short list in order costs one
     * failed connect and is the whole mechanism.
     */
    private suspend fun resolve(): String {
        host?.let { return it }
        // A short-timeout clone for probing only. The real client waits five
        // minutes on a read, because an escalation to the model genuinely
        // takes that long — but a *probe* that waits eight seconds per dead
        // address makes toggling a VPN feel like the app has hung, when all
        // it is doing is working down a list. The connection pool and the TLS
        // material are shared; only the clocks differ.
        val probe = http.newBuilder()
            .connectTimeout(2500, TimeUnit.MILLISECONDS)
            .readTimeout(2500, TimeUnit.MILLISECONDS)
            .build()
        var last: Exception? = null
        for (candidate in server.candidates()) {
            try {
                probe.newCall(Request.Builder()
                    .url("${server.base(candidate)}/v1/hello").get().build())
                    .execute().close()
                host = candidate
                return candidate
            } catch (e: Exception) {
                last = e
            }
        }
        throw last ?: IOException("V is not reachable on any known address")
    }

    /** Forget the working address — called when a request fails, so the next
     *  one re-probes rather than hammering an address that has moved. */
    fun forgetHost() { host = null }

    /**
     * The base URL, resolved on the IO dispatcher.
     *
     * `withContext` here is not decoration. `resolve()` opens sockets, and the
     * one caller that reaches it from outside an already-IO block is
     * `VService`'s reconnect loop, which runs on `lifecycleScope` — the main
     * thread. That threw `NetworkOnMainThreadException` into a `catch
     * (Exception)` that existed for dropped connections, so the event stream
     * retried forever, backed off to a minute, and never once connected, while
     * chat worked perfectly because every chat call was already wrapped. The
     * symptom was a phone that answered questions and never delivered a
     * reminder. Nesting IO inside IO costs nothing.
     */
    suspend fun base(): String = withContext(Dispatchers.IO) {
        server.base(resolve())
    }

    // ---- endpoints -------------------------------------------------------
    suspend fun hello(): Hello = withContext(Dispatchers.IO) {
        val o = JSONObject(body(get("${base()}/v1/hello")))
        Hello(o.optInt("notes"), o.optBoolean("tts"), o.optBoolean("stt"),
              o.optString("device"), o.optLong("seq"))
    }

    /**
     * The home-screen snapshot — everything both widgets draw, in one call.
     *
     * Returned raw rather than parsed into a `Turn`, because it is not a turn:
     * nothing was said, nothing was spent, and there is no cost line to render.
     * `Snapshot` owns the shape from here.
     */
    suspend fun glance(): JSONObject = withContext(Dispatchers.IO) {
        JSONObject(body(get("${base()}/v1/glance")))
    }

    /** [fileRel] is a path `attach` just returned. Two calls rather than one
     *  multipart request — see `httpapi.h_chat` for why the caption cannot
     *  ride along with the bytes. */
    suspend fun chat(text: String, fileRel: String = ""): Turn =
        withContext(Dispatchers.IO) {
            val payload = JSONObject().put("text", text)
            if (fileRel.isNotBlank()) payload.put("file_rel", fileRel)
            Turn.parse(JSONObject(body(post("${base()}/v1/chat", payload))))
        }

    /**
     * Upload bytes; get back where they landed in the vault.
     *
     * The name goes in the query string because it is the one piece of
     * metadata that survives percent-encoding intact — including the Persian
     * filenames half of these have. The bytes are the whole body, exactly as
     * `voice` posts a WAV, so neither end needs a multipart parser.
     */
    suspend fun attach(data: ByteArray, name: String, mime: String): VFile =
        withContext(Dispatchers.IO) {
            val q = java.net.URLEncoder.encode(name, "UTF-8")
            val req = Request.Builder()
                .url("${base()}/v1/attach?name=$q")
                .post(data.toRequestBody(mime.toMediaType()))
                .build()
            VFile.parse(JSONObject(body(http.newCall(req).execute())))
        }

    /** The bytes of a file already in the vault. Streamed to [sink] rather
     *  than returned, so a 40 MB video is not held in the heap twice. */
    suspend fun download(rel: String, sink: java.io.OutputStream): Long =
        withContext(Dispatchers.IO) {
            val q = java.net.URLEncoder.encode(rel, "UTF-8")
            get("${base()}/v1/file?rel=$q").use { r ->
                check(r)
                r.body?.byteStream()?.copyTo(sink) ?: 0L
            }
        }

    /** WAV in, an answer plus the transcript out. See `Recorder` for why WAV. */
    suspend fun voice(wav: ByteArray): Turn = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url("${base()}/v1/voice")
            .post(wav.toRequestBody("audio/wav".toMediaType()))
            .build()
        Turn.parse(JSONObject(body(http.newCall(req).execute())))
    }

    suspend fun speak(text: String): ByteArray = withContext(Dispatchers.IO) {
        val payload = JSONObject().put("text", text)
        post("${base()}/v1/speak", payload).use { r ->
            check(r)
            r.body?.bytes() ?: ByteArray(0)
        }
    }

    suspend fun act(button: Button): Turn = withContext(Dispatchers.IO) {
        val payload = JSONObject()
            .put("action", button.action)
            .put("payload", JSONObject(button.payload))
        Turn.parse(JSONObject(body(post("${base()}/v1/act", payload))))
    }

    suspend fun newSession(): Unit = withContext(Dispatchers.IO) {
        body(post("${base()}/v1/session/new", JSONObject()))
    }

    /** [agent] is the name the gate heard. It decides who answers — see
     *  `httpapi.h_wake` for why that is a persona switch and not a label. */
    suspend fun wake(source: String, agent: String = ""): Turn = withContext(Dispatchers.IO) {
        val q = StringBuilder("${base()}/v1/wake?source=")
            .append(java.net.URLEncoder.encode(source, "UTF-8"))
        if (agent.isNotBlank()) {
            q.append("&agent=").append(java.net.URLEncoder.encode(agent, "UTF-8"))
        }
        Turn.parse(JSONObject(body(post(q.toString(), JSONObject()))))
    }

    suspend fun eventsUrl(since: Long): String = "${base()}/v1/events?since=$since"

    // ---- plumbing --------------------------------------------------------
    private fun get(url: String): Response =
        http.newCall(Request.Builder().url(url).get().build()).execute()

    private fun post(url: String, payload: JSONObject): Response =
        http.newCall(Request.Builder().url(url)
            .post(payload.toString().toRequestBody(json)).build()).execute()

    private fun body(response: Response): String = response.use { r ->
        check(r)
        r.body?.string().orEmpty()
    }

    private fun check(r: Response) {
        if (r.isSuccessful) return
        forgetHost()
        // The server answers a failure as `{"error": ..., "status": ...}`, and
        // that sentence is written for a person — "this device is no longer
        // enrolled" is the one the phone most needs to show verbatim rather
        // than flatten into "network error".
        val message = try {
            JSONObject(r.peekBody(4096).string()).optString("error", r.message)
        } catch (e: Exception) {
            r.message
        }
        throw ApiException(r.code, message)
    }
}
