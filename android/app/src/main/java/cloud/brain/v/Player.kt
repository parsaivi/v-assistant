package cloud.brain.v

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import java.io.File
import kotlin.coroutines.resume

/**
 * V's voice, coming out of wherever the phone's audio is already going.
 *
 * The audio is OGG/Opus straight from Piper on the laptop — `tts.to_ogg_opus`
 * already existed for Telegram's voice messages, and Android has decoded Opus
 * in Ogg since Lollipop, so nothing transcodes anywhere in this path.
 *
 * This class used to carry a bluetooth SCO link, because the microphone used
 * to be the headset's. It is not any more: the gate listens on the phone, so
 * A2DP is never torn down, music keeps playing at full quality, and routing
 * stops being a decision this code makes at all — connected headphones get the
 * reply because they already have everything else.
 *
 * What replaced it is audio focus. A reply spoken over a podcast at full volume
 * is unintelligible, so a turn takes transient focus and lets the system duck
 * whatever is playing for its duration. That is one call rather than a profile
 * state machine, and unlike the SCO version it cannot leave the phone stuck in
 * communication mode if the process dies mid-turn.
 */
class Player(private val context: Context) {

    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private var player: MediaPlayer? = null

    /**
     * Play [ogg] and suspend until it finishes or fails.
     *
     * On the IO dispatcher because both `writeBytes` and `MediaPlayer.prepare`
     * block, and every caller reaches this from a UI or lifecycle scope — the
     * main thread. A ~200KB write plus a decoder set-up is small enough to get
     * away with most of the time, which is the worst property a main-thread
     * block can have: it works until the one reply that is long enough to ANR.
     */
    suspend fun play(ogg: ByteArray): Unit =
        withContext(Dispatchers.IO) { playBlocking(ogg) }

    private suspend fun playBlocking(ogg: ByteArray): Unit =
        suspendCancellableCoroutine { cont ->
            stop()
            val file = File(context.cacheDir, "v-reply.ogg").apply {
                writeBytes(ogg)
                deleteOnExit()
            }
            val mp = MediaPlayer()
            player = mp
            try {
                mp.setAudioAttributes(
                    AudioAttributes.Builder()
                        // USAGE_MEDIA, which is what this was before the SCO
                        // removal and what it should have stayed. Dropping the
                        // `overHeadset` branch was right — the gate listens on
                        // the phone, so A2DP already carries a reply to any
                        // connected headphones and there is no route to choose.
                        // Switching the survivor to USAGE_ASSISTANT was not: it
                        // bought nothing, and an assistant-usage stream is at
                        // the mercy of per-device policy about where assistant
                        // audio goes and how loud. MEDIA plays at media volume
                        // like every other sound the phone makes, which is the
                        // original reasoning and still correct.
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build())
                mp.setDataSource(file.absolutePath)
                mp.setOnCompletionListener {
                    it.release()
                    if (player === it) player = null
                    if (cont.isActive) cont.resume(Unit)
                }
                mp.setOnErrorListener { p, _, _ ->
                    p.release()
                    if (player === p) player = null
                    if (cont.isActive) cont.resume(Unit)
                    true
                }
                mp.prepare()
                mp.start()
            } catch (e: Exception) {
                // Degrade, never crash: the words are already on screen, which
                // is the same posture `telegram._send_voice` takes when Piper
                // is missing on the other end.
                try { mp.release() } catch (ignored: Exception) { }
                player = null
                if (cont.isActive) cont.resume(Unit)
            }
            cont.invokeOnCancellation { stop() }
        }

    fun stop() {
        player?.let {
            try { it.stop() } catch (e: Exception) { /* not started */ }
            it.release()
        }
        player = null
    }

    // ---- audio focus ------------------------------------------------------
    private var focus: android.media.AudioFocusRequest? = null

    /** Duck whatever is playing for the length of a spoken turn. */
    fun takeFocus() {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.O) return
        if (focus != null) return
        val req = android.media.AudioFocusRequest.Builder(
                android.media.AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ASSISTANT)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build())
            .build()
        try { audio.requestAudioFocus(req); focus = req } catch (e: Exception) { }
    }

    fun releaseFocus() {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.O) return
        focus?.let { try { audio.abandonAudioFocusRequest(it) } catch (e: Exception) { } }
        focus = null
    }

}
