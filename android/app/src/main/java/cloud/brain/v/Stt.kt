package cloud.brain.v

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognitionSupport
import android.speech.RecognitionSupportCallback
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import java.util.Locale
import kotlin.coroutines.resume

/**
 * Transcription on the phone, so the audio never has to travel at all.
 *
 * ### Why this exists
 *
 * Measured on Parsa's laptop, Whisper `large-v3` on an i7-1355U runs at ~8x
 * realtime: 3.2 seconds of speech cost 25.5 seconds to transcribe, and none of
 * it started until the recording had been uploaded. That was ~25 of the ~35
 * seconds a phone turn took. Every other fix in this file's neighbourhood —
 * a smaller model, chunking the upload, streaming the reply — makes that
 * number smaller. This one deletes it: a Pixel's own recognizer runs on
 * hardware built for it, returns partial results *while* he is still talking,
 * and by the time the button comes up the text is already there.
 *
 * It also removes the upload. `Recorder` produces 32KB per second of raw PCM;
 * a fifteen-second memo is ~480KB that no longer crosses the network.
 *
 * ### Why it is not simply switched on
 *
 * On-device recognizers are single-language per session, and Parsa's vault is
 * bilingual — his tasks are things like `کال کردن RPC در سابمیت`. A recognizer
 * running an English pack against Persian speech does not fail, it *invents*,
 * and a confidently wrong transcript is worse than a slow correct one: the
 * slow path is visibly slow, while the wrong one is silently wrong and reaches
 * the vault.
 *
 * So the rule is conservative on purpose. On-device is used only when the
 * platform states, through [checkRecognitionSupport], that the chosen language
 * is installed *on the device* — not merely supported by some cloud service —
 * and [enabled] lets Parsa switch it off wholesale the first time it mangles
 * a sentence. Everything else falls through to the streamed Whisper path,
 * which is bilingual because `whisper_language` is left unset.
 */
class Stt(private val context: Context) {

    companion object {
        /** Its own file, not the encrypted voiceprint store: a locale name is
         *  not a secret, and putting it there would mean a keystore round trip
         *  on a decision made before every turn. */
        private const val PREFS = "v-stt"
        private const val K_ENABLED = "on_device"
        private const val K_LOCALE = "locale"

        /** The recognizer arrived in API 31; below that there is nothing to
         *  ask about. */
        val supported: Boolean get() = Build.VERSION.SDK_INT >= Build.VERSION_CODES.S

        fun installed(context: Context): Boolean =
            supported && SpeechRecognizer.isOnDeviceRecognitionAvailable(context)
    }

    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Off switch, for the day it mishears Persian as English. Defaults on:
     *  the failure is visible in the transcript line the chat log already
     *  shows, which is the whole reason that line is rendered. */
    var enabled: Boolean
        get() = prefs.getBoolean(K_ENABLED, true)
        set(v) = prefs.edit().putBoolean(K_ENABLED, v).apply()

    /** Which language pack to use. The phone's own locale unless overridden —
     *  and it is worth overriding, because the answer for a Persian speaker
     *  whose phone is in English is not the phone's locale. */
    var locale: String
        get() = prefs.getString(K_LOCALE, null) ?: Locale.getDefault().toLanguageTag()
        set(v) = prefs.edit().putString(K_LOCALE, v).apply()

    /**
     * Can this phone transcribe [locale] itself, right now, offline?
     *
     * Answered by the platform rather than assumed. Below API 33 there is no
     * way to ask which packs are downloaded — only whether a recognizer
     * exists — and "exists" is not the question, so the honest answer there is
     * no and the server path is used.
     */
    suspend fun usable(): Boolean = withContext(Dispatchers.Main) {
        if (!enabled || !installed(context)) return@withContext false
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return@withContext false
        suspendCancellableCoroutine { cont ->
            val recognizer = try {
                SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
            } catch (e: Exception) {
                if (cont.isActive) cont.resume(false)
                return@suspendCancellableCoroutine
            }
            val want = locale
            fun finish(answer: Boolean) {
                try { recognizer.destroy() } catch (e: Exception) { }
                if (cont.isActive) cont.resume(answer)
            }
            try {
                recognizer.checkRecognitionSupport(
                    intent(want), context.mainExecutor,
                    object : RecognitionSupportCallback {
                        override fun onSupportResult(support: RecognitionSupport) {
                            // `supportedOnDeviceLanguages` is the only list that
                            // means "will work with the network off". The other
                            // two are things the platform could fetch or could
                            // route to a server, and either would put this back
                            // where it started.
                            val have = support.supportedOnDeviceLanguages
                            finish(have.any { it.equals(want, true) ||
                                              it.startsWith(want.take(2), true) })
                        }
                        override fun onError(error: Int) = finish(false)
                    })
            } catch (e: Exception) {
                finish(false)
            }
            cont.invokeOnCancellation { try { recognizer.destroy() } catch (e: Exception) { } }
        }
    }

    /**
     * Listen until [stop] is called, reporting text as it forms.
     *
     * Returns the final transcript, or null if the recognizer failed — in
     * which case the caller must fall back rather than treat it as silence.
     * A recognizer that heard nothing returns "", which is a different thing
     * and is not a failure: a button brushed is an ordinary thing to do.
     *
     * The whole body runs on the main thread because `SpeechRecognizer` is
     * documented to require it and misbehaves quietly rather than loudly when
     * it does not get it.
     */
    suspend fun listen(
        onPartial: (String) -> Unit = {},
        onLevel: (Float) -> Unit = {},
        register: (Stopper) -> Unit = {},
    ): String? = withContext(Dispatchers.Main) {
        suspendCancellableCoroutine { cont ->
            val recognizer = try {
                SpeechRecognizer.createOnDeviceSpeechRecognizer(context)
            } catch (e: Exception) {
                if (cont.isActive) cont.resume(null)
                return@suspendCancellableCoroutine
            }
            var settled = false
            fun settle(text: String?) {
                if (settled) return
                settled = true
                try { recognizer.destroy() } catch (e: Exception) { }
                if (cont.isActive) cont.resume(text)
            }
            recognizer.setRecognitionListener(object : RecognitionListener {
                override fun onResults(results: Bundle) =
                    settle(best(results) ?: "")

                override fun onPartialResults(partial: Bundle) {
                    best(partial)?.let(onPartial)
                }

                override fun onError(error: Int) {
                    // "No match" and "speech timeout" mean he said nothing,
                    // which is a transcript of "" — not a reason to fall back
                    // to uploading the silence and asking a model about it.
                    settle(when (error) {
                        SpeechRecognizer.ERROR_NO_MATCH,
                        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> ""
                        else -> null
                    })
                }

                /** The waveform's 21 bars are specified to be real mic RMS.
                 *  This recognizer owns the microphone while it runs, so this
                 *  callback is the only place that signal exists. */
                override fun onRmsChanged(rmsdB: Float) {
                    // The callback is in dB over an unspecified reference,
                    // roughly -2..10 in practice. Mapped to the same 0..1 the
                    // recorder's RMS produces so `Waveform` cannot tell which
                    // path is feeding it.
                    onLevel(((rmsdB + 2f) / 12f).coerceIn(0f, 1f))
                }

                override fun onReadyForSpeech(params: Bundle?) { }
                override fun onBeginningOfSpeech() { }
                override fun onEndOfSpeech() { }
                override fun onBufferReceived(buffer: ByteArray?) { }
                override fun onEvent(eventType: Int, params: Bundle?) { }
            })
            register(Stopper {
                // `stopListening` asks for a final result over what it has;
                // `cancel` would throw the sentence away. The finger coming
                // off the button means "I have finished", not "forget it".
                try { recognizer.stopListening() } catch (e: Exception) { settle(null) }
            })
            try {
                recognizer.startListening(intent(locale))
            } catch (e: Exception) {
                settle(null)
            }
            cont.invokeOnCancellation {
                try { recognizer.cancel() } catch (e: Exception) { }
                try { recognizer.destroy() } catch (e: Exception) { }
            }
        }
    }

    /** Handed back to the caller so the button's release can end the turn. */
    fun interface Stopper { fun stop() }

    private fun best(bundle: Bundle): String? =
        bundle.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            ?.firstOrNull()?.trim()

    private fun intent(want: String) =
        Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                     RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, want)
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            // Belt and braces with `createOnDeviceSpeechRecognizer`: the flag
            // is what makes the guarantee explicit in the request itself, so a
            // future path that builds a plain recognizer cannot quietly start
            // sending his voice to a transcription service.
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        }
}
