package cloud.brain.v

import android.content.Context
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.cos
import kotlin.math.ln
import kotlin.math.sqrt

/**
 * The feature front-end for the speaker model: audio in, log-mel out.
 *
 * This file is the highest-risk code in the gate, and the reason is worth
 * stating plainly. A speaker embedding is only meaningful against embeddings
 * computed the *same* way; if these numbers drift a few percent from what the
 * model saw in training, the network still returns 192 confident-looking
 * floats and they are quietly worthless. Nothing crashes. The gate just stops
 * recognising Parsa, and there is no error to read.
 *
 * So two decisions here are about removing chances to be wrong rather than
 * about speed:
 *
 *  1. **The mel filterbank is shipped, not computed.** `models/mel80.f32` is
 *     the exact 80×257 matrix `librosa.filters.mel(norm="slaney")` produces,
 *     dumped to float32 on the laptop. Slaney-normalised mel has two breakpoint
 *     conventions and an area-normalisation step that are easy to get subtly
 *     wrong, and a subtly wrong filterbank is precisely the failure above. 82KB
 *     buys certainty.
 *  2. **Everything else mirrors NeMo's featurizer step for step**, because
 *     TitaNet was trained on it: pre-emphasis 0.97 → STFT(512, hop 160, win
 *     400, periodic hann, reflect-padded) → power → mel → log(x + 2⁻²⁴) →
 *     per-feature mean/variance normalisation over time.
 *
 * The reference implementation these were checked against lives in the design
 * note; on two speakers it separated same from different by a margin of 0.29,
 * which is the number that says the chain is right.
 */
object Features {

    const val N_FFT = 512
    const val HOP = 160          // 10ms at 16kHz
    const val WIN = 400          // 25ms
    const val N_MELS = 80
    const val BINS = N_FFT / 2 + 1   // 257

    private const val PREEMPH = 0.97f
    private const val LOG_GUARD = 5.9604645e-8f   // 2^-24, NeMo's log_zero_guard_value

    /** The periodic hann window of length [WIN], zero-padded into [N_FFT] and
     *  centred — librosa pads a short `win_length` symmetrically inside the
     *  FFT frame rather than left-aligning it. */
    private val window: FloatArray = FloatArray(N_FFT).also { w ->
        val off = (N_FFT - WIN) / 2
        for (n in 0 until WIN) {
            w[off + n] = (0.5 - 0.5 * cos(2.0 * Math.PI * n / WIN)).toFloat()
        }
    }

    @Volatile private var mel: Array<FloatArray>? = null

    /** Load the shipped filterbank once. Cheap, but not free — 82KB parsed on
     *  every gate start would be 82KB parsed every time a headset moves. */
    fun load(context: Context) {
        if (mel != null) return
        context.assets.open("models/mel80.f32").use { load(it) }
    }

    /** The same load, from any stream — which is what lets the JVM test run
     *  this file against golden vectors without an emulator. */
    fun load(stream: java.io.InputStream) {
        synchronized(this) {
            if (mel != null) return
            val buf = ByteBuffer.wrap(stream.readBytes())
                .order(ByteOrder.LITTLE_ENDIAN).asFloatBuffer()
            val m = Array(N_MELS) { FloatArray(BINS) }
            for (r in 0 until N_MELS) buf.get(m[r])
            mel = m
        }
    }

    /**
     * NeMo log-mel features for [pcm] (mono 16kHz, scaled to ±1).
     *
     * Returns `[N_MELS][frames]` — mel-major, which is the layout TitaNet's
     * `audio_signal` input wants, so nothing is transposed afterwards.
     */
    fun logMel(pcm: FloatArray): Array<FloatArray> {
        val bank = mel ?: error("Features.load() was never called")

        // ---- pre-emphasis ----
        val x = FloatArray(pcm.size)
        if (pcm.isNotEmpty()) {
            x[0] = pcm[0]
            for (i in 1 until pcm.size) x[i] = pcm[i] - PREEMPH * pcm[i - 1]
        }

        // ---- reflect-pad by N_FFT/2, which is what center=True means ----
        // numpy's "reflect" mirrors *without* repeating the edge sample, so
        // padded[i] = x[pad - i]. Getting this off by one shifts every frame
        // by a sample and quietly changes every feature.
        val pad = N_FFT / 2
        val padded = FloatArray(x.size + 2 * pad)
        for (i in 0 until pad) {
            padded[i] = x[(pad - i).coerceIn(0, x.size - 1)]
            padded[padded.size - 1 - i] = x[(x.size - 1 - (pad - i)).coerceIn(0, x.size - 1)]
        }
        System.arraycopy(x, 0, padded, pad, x.size)

        val frames = 1 + x.size / HOP
        val out = Array(N_MELS) { FloatArray(frames) }

        val re = FloatArray(N_FFT)
        val im = FloatArray(N_FFT)
        val power = FloatArray(BINS)

        for (t in 0 until frames) {
            val start = t * HOP
            for (i in 0 until N_FFT) {
                re[i] = padded[start + i] * window[i]
                im[i] = 0f
            }
            fft(re, im)
            for (k in 0 until BINS) power[k] = re[k] * re[k] + im[k] * im[k]
            for (m in 0 until N_MELS) {
                val row = bank[m]
                var acc = 0f
                for (k in 0 until BINS) acc += row[k] * power[k]
                out[m][t] = ln(acc + LOG_GUARD)
            }
        }

        // ---- per-feature normalisation over time ----
        // Per mel bin, not per utterance: it is what makes the embedding
        // insensitive to how loud he was and how far from the phone.
        for (m in 0 until N_MELS) {
            val row = out[m]
            var mean = 0f
            for (v in row) mean += v
            mean /= frames
            var varr = 0f
            for (v in row) { val d = v - mean; varr += d * d }
            val sd = sqrt(varr / frames) + 1e-5f
            for (i in row.indices) row[i] = (row[i] - mean) / sd
        }
        return out
    }

    /**
     * In-place iterative radix-2 FFT. [N_FFT] is 512, a power of two, so the
     * general case never arises and a library would be 200KB to avoid 40 lines.
     */
    private fun fft(re: FloatArray, im: FloatArray) {
        val n = re.size
        var j = 0
        for (i in 1 until n) {
            var bit = n shr 1
            while (j and bit != 0) { j = j xor bit; bit = bit shr 1 }
            j = j or bit
            if (i < j) {
                var t = re[i]; re[i] = re[j]; re[j] = t
                t = im[i]; im[i] = im[j]; im[j] = t
            }
        }
        var len = 2
        while (len <= n) {
            val ang = -2.0 * Math.PI / len
            val wr = cos(ang).toFloat()
            val wi = kotlin.math.sin(ang).toFloat()
            var i = 0
            while (i < n) {
                var cr = 1f; var ci = 0f
                for (k in 0 until len / 2) {
                    val ur = re[i + k];          val ui = im[i + k]
                    val vr = re[i + k + len / 2] * cr - im[i + k + len / 2] * ci
                    val vi = re[i + k + len / 2] * ci + im[i + k + len / 2] * cr
                    re[i + k] = ur + vr;         im[i + k] = ui + vi
                    re[i + k + len / 2] = ur - vr; im[i + k + len / 2] = ui - vi
                    val ncr = cr * wr - ci * wi
                    ci = cr * wi + ci * wr
                    cr = ncr
                }
                i += len
            }
            len = len shl 1
        }
    }

    /** L2-normalise in place and return, so cosine is a plain dot product. */
    fun l2(v: FloatArray): FloatArray {
        var s = 0f
        for (x in v) s += x * x
        val n = sqrt(s)
        if (n > 1e-9f) for (i in v.indices) v[i] /= n
        return v
    }

    fun cosine(a: FloatArray, b: FloatArray): Float {
        var s = 0f
        for (i in a.indices) s += a[i] * b[i]
        return s
    }
}
