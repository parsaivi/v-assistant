package cloud.brain.v

import android.content.Context
import android.content.SharedPreferences
import android.util.Base64
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * What the gate knows about Parsa: the wake-word templates and the speaker
 * centroid, plus the two thresholds they are compared against.
 *
 * **Encrypted, and it is not theatre.** A speaker centroid is biometric data.
 * It never leaves the phone — it is not sent to the laptop, is not in the
 * `allowBackup=false` backup that does not happen anyway, and is Keystore-
 * wrapped so that a stolen phone's `/data` dump is 192 floats of nothing.
 * This is the same argument the app's private key gets in the manifest, and it
 * applies with more force here: he can rotate a key.
 *
 * **On the thresholds being stored rather than compiled in.** They are the one
 * part of this design that cannot be reasoned to a correct value. Two gates in
 * series multiply false rejections, and the honest way to set them is against
 * the distribution of scores from the room Parsa actually sits in — which is
 * what [shadowMode] collects. Constants in a source file would be a guess
 * wearing a number's clothes.
 */
class Voiceprint(context: Context, file: String = FILE) {

    companion object {
        /** The real store. Tests pass their own name — see the constructor. */
        const val FILE = "v-voiceprint"
        private const val K_TEMPLATES = "wake.templates"
        private const val K_CENTROID = "speaker.centroid"
        private const val K_WAKE_T = "wake.threshold"
        private const val K_SPEAKER_T = "speaker.threshold"
        private const val K_SHADOW = "shadow"
        private const val K_LISTENING = "listening"
        private const val K_LOG = "shadow.log"

        /** Starting points, not answers. The wake figure is deliberately loose
         *  and the speaker figure deliberately tight: a missed wake word costs
         *  one repeat, a false accept means the phone answered someone else. */
        const val DEFAULT_WAKE = 0.62f
        const val DEFAULT_SPEAKER = 0.55f

        /** Enough utterances to average out one bad take, few enough that
         *  enrollment stays under a minute. */
        const val ENROLL_TARGET = 15

        /** Kept small on purpose: this is a tuning aid, not a recording of his
         *  life. Scores only — no audio is ever written to it. */
        private const val LOG_CAP = 400
    }

    /**
     * [file] exists so an instrumented test can exercise this class without
     * writing into Parsa's actual voiceprint.
     *
     * That is not hypothetical caution. A storage test that used the default
     * store left his `shadowMode` flipped and appended a fabricated entry to
     * his shadow log — the log whose whole purpose is to be an honest record of
     * what the phone heard. A test that corrupts the data it is verifying is
     * worse than no test.
     */
    private val prefs: SharedPreferences = run {
        val key = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context, file, key,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM)
    }

    // ---- enrollment -------------------------------------------------------
    val isEnrolled: Boolean
        get() = prefs.contains(K_CENTROID) && templates().isNotEmpty()

    fun templates(): List<FloatArray> =
        prefs.getStringSet(K_TEMPLATES, emptySet())!!.map { decode(it) }

    fun centroid(): FloatArray? = prefs.getString(K_CENTROID, null)?.let { decode(it) }

    /**
     * Commit an enrollment.
     *
     * The centroid is the mean of the accepted embeddings, re-normalised. Takes
     * more than [ENROLL_TARGET] samples happily — [Enrollment] drops outliers
     * before calling this, and how many it drops depends on how many were
     * coughed through.
     */
    fun enroll(wakeTemplates: List<FloatArray>, speakerEmbeddings: List<FloatArray>) {
        require(wakeTemplates.isNotEmpty() && speakerEmbeddings.isNotEmpty()) {
            "cannot enroll on nothing"
        }
        val c = FloatArray(SpeakerId.DIMS)
        for (e in speakerEmbeddings) for (i in c.indices) c[i] += e[i]
        for (i in c.indices) c[i] /= speakerEmbeddings.size
        prefs.edit()
            .putStringSet(K_TEMPLATES, wakeTemplates.map { encode(it) }.toSet())
            .putString(K_CENTROID, encode(Features.l2(c)))
            .apply()
    }

    fun forget() {
        prefs.edit().remove(K_TEMPLATES).remove(K_CENTROID).remove(K_LOG).apply()
    }

    // ---- thresholds -------------------------------------------------------
    var wakeThreshold: Float
        get() = prefs.getFloat(K_WAKE_T, DEFAULT_WAKE)
        set(v) = prefs.edit().putFloat(K_WAKE_T, v).apply()

    var speakerThreshold: Float
        get() = prefs.getFloat(K_SPEAKER_T, DEFAULT_SPEAKER)
        set(v) = prefs.edit().putFloat(K_SPEAKER_T, v).apply()

    /**
     * The off switch for the always-on microphone.
     *
     * Not a debug flag — the single most important control in the app. A thing
     * that holds the microphone for every waking hour has to have an obvious,
     * one-tap "stop", reachable without opening anything, or the honest
     * description of it is spyware with good intentions. It lives here rather
     * than in a settings screen because the notification is where it belongs:
     * that is the surface that is visible precisely when the app is not.
     *
     * Persisted, so turning it off survives a reboot. Someone who switched the
     * microphone off does not expect it back on in the morning.
     */
    var listeningEnabled: Boolean
        get() = prefs.getBoolean(K_LISTENING, true)
        set(v) = prefs.edit().putBoolean(K_LISTENING, v).apply()

    /** While true the gate scores everything and acts on nothing. */
    var shadowMode: Boolean
        get() = prefs.getBoolean(K_SHADOW, true)
        set(v) = prefs.edit().putBoolean(K_SHADOW, v).apply()

    // ---- the shadow log ---------------------------------------------------
    /** One line per candidate segment: when, how long, and the two scores. */
    fun record(durationMs: Int, wake: Float, speaker: Float?) {
        val line = "${System.currentTimeMillis()},$durationMs," +
                   "${"%.4f".format(wake)},${speaker?.let { "%.4f".format(it) } ?: ""}"
        val log = prefs.getString(K_LOG, "").orEmpty()
            .split('\n').filter { it.isNotBlank() }.toMutableList()
        log.add(line)
        while (log.size > LOG_CAP) log.removeAt(0)
        prefs.edit().putString(K_LOG, log.joinToString("\n")).apply()
    }

    fun log(): List<ShadowEntry> =
        prefs.getString(K_LOG, "").orEmpty().split('\n')
            .filter { it.isNotBlank() }
            .mapNotNull { line ->
                val p = line.split(',')
                if (p.size < 4) return@mapNotNull null
                ShadowEntry(
                    at = p[0].toLongOrNull() ?: return@mapNotNull null,
                    durationMs = p[1].toIntOrNull() ?: 0,
                    wake = p[2].toFloatOrNull() ?: return@mapNotNull null,
                    speaker = p[3].toFloatOrNull())
            }

    fun clearLog() = prefs.edit().remove(K_LOG).apply()

    // ---- float[] <-> string ----------------------------------------------
    private fun encode(v: FloatArray): String {
        val b = ByteBuffer.allocate(v.size * 4).order(ByteOrder.LITTLE_ENDIAN)
        for (x in v) b.putFloat(x)
        return Base64.encodeToString(b.array(), Base64.NO_WRAP)
    }

    private fun decode(s: String): FloatArray {
        val b = ByteBuffer.wrap(Base64.decode(s, Base64.NO_WRAP))
            .order(ByteOrder.LITTLE_ENDIAN)
        return FloatArray(b.remaining() / 4) { b.float }
    }
}

data class ShadowEntry(
    val at: Long,
    val durationMs: Int,
    val wake: Float,
    val speaker: Float?,
)
