package cloud.brain.v

import ai.onnxruntime.OnnxTensor
import ai.onnxruntime.OrtEnvironment
import ai.onnxruntime.OrtSession
import android.content.Context
import java.nio.FloatBuffer

/**
 * Shared ONNX Runtime plumbing.
 *
 * One `OrtEnvironment` for the process — it owns thread pools and arenas, and
 * two of them on a phone means two of everything for no gain.
 *
 * Every session is pinned to **one intra-op thread**. That is deliberate and
 * against the usual advice: the gate runs on a small, fixed budget once every
 * 32ms, forever, and the thing being optimised is not latency but the number
 * of cores kept out of deep sleep. Letting ORT fan a 2ms convolution across
 * four cores wakes four cores 30 times a second and shows up as battery, which
 * is the resource this feature actually spends.
 */
object Onnx {

    val env: OrtEnvironment by lazy { OrtEnvironment.getEnvironment() }

    fun session(context: Context, asset: String): OrtSession {
        val bytes = context.assets.open(asset).use { it.readBytes() }
        val opts = OrtSession.SessionOptions().apply {
            setIntraOpNumThreads(1)
            setInterOpNumThreads(1)
            setOptimizationLevel(OrtSession.SessionOptions.OptLevel.ALL_OPT)
        }
        return env.createSession(bytes, opts)
    }

    fun tensor(data: FloatArray, vararg shape: Long): OnnxTensor =
        OnnxTensor.createTensor(env, FloatBuffer.wrap(data), shape)

    /** Flatten `[rows][cols]` row-major, which is what every input here wants. */
    fun flatten(m: Array<FloatArray>): FloatArray {
        val rows = m.size
        val cols = if (rows == 0) 0 else m[0].size
        val out = FloatArray(rows * cols)
        for (r in 0 until rows) System.arraycopy(m[r], 0, out, r * cols, cols)
        return out
    }
}
