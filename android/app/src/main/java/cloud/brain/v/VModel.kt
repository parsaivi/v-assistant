package cloud.brain.v

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * One conversation, and the ambient state that reads off it.
 *
 * `Tui.ambient()` on the terminal surface *reads* V's state from things that
 * are already true rather than being told it — thinking while a turn is in
 * flight, speaking for as long as the answer takes to read — precisely so that
 * nothing has to remember to clear a flag. The same rule holds here: every
 * transition below is a consequence of work starting or finishing, and there
 * is no `setState` anyone can call from outside.
 */
class VModel(app: Application) : AndroidViewModel(app) {

    private val api = Api(app)
    private val recorder = Recorder()
    private val player = Player(app)
    private val stt = Stt(app)
    private val speech = Speech(api, player)
    private val dictation = Dictation(api, recorder)

    /** The live transcript while he is still talking — the phone's own
     *  recognizer produces this as the words form, and it is shown in place of
     *  the composer's placeholder so a wrong hearing is visible before the
     *  turn is spent on it rather than after. */
    private val _heard = MutableStateFlow("")
    val heard: StateFlow<String> = _heard.asStateFlow()

    private val _turns = MutableStateFlow<List<Turn>>(emptyList())
    val turns: StateFlow<List<Turn>> = _turns.asStateFlow()

    private val _ambient = MutableStateFlow(Ambient.IDLE)
    val ambient: StateFlow<Ambient> = _ambient.asStateFlow()

    private val _status = MutableStateFlow("")
    val status: StateFlow<String> = _status.asStateFlow()

    private val _notes = MutableStateFlow(0)
    val notes: StateFlow<Int> = _notes.asStateFlow()

    /** Non-null when something has gone wrong that Parsa should see once. An
     *  error is amber, never red — red is for refusals, and teaching the eye to
     *  read one as the other means the refusal is the one that gets ignored. */
    private val _fault = MutableStateFlow<String?>(null)
    val fault: StateFlow<String?> = _fault.asStateFlow()

    /** The waveform's 21 bars, from whichever path is holding the microphone
     *  — the recorder's own RMS, or the recognizer's, mapped to the same
     *  scale. The brief requires these to be the real signal in production,
     *  so neither path is allowed to fake them while the other is running.
     *
     *  Mirrored out of the recorder rather than exposed directly, because on
     *  the on-device route the recorder is not running at all and the signal
     *  arrives through `Stt`'s RMS callback instead. */
    private val _levels = MutableStateFlow(FloatArray(Recorder.BARS))
    val levels: StateFlow<FloatArray> = _levels.asStateFlow()

    /**
     * The file picked but not yet sent.
     *
     * It is uploaded the moment it is picked, not when Send is pressed —
     * so the wait happens while the caption is being typed rather than after
     * it, and so a failed upload is discovered before the sentence describing
     * it has been written. `pending.rel` is a real vault path by the time this
     * is non-null; the composer is showing a receipt, not an intention.
     */
    private val _pending = MutableStateFlow<VFile?>(null)
    val pending: StateFlow<VFile?> = _pending.asStateFlow()

    /** Non-null while a file is on the wire, either direction — the composer
     *  and the chip both need to say so rather than looking inert. */
    private val _busyFile = MutableStateFlow<String?>(null)
    val busyFile: StateFlow<String?> = _busyFile.asStateFlow()

    private var turnJob: Job? = null
    private var speakJob: Job? = null

    init {
        refresh()
        viewModelScope.launch { recorder.levels.collect { _levels.value = it } }
    }

    fun refresh() = viewModelScope.launch {
        try {
            // Generate the key if this is the first run, publish its public
            // half, and pick up a certificate if one has been pushed since the
            // last look. Cheap and idempotent, so it runs on every refresh
            // rather than needing a moment someone has to remember.
            val was = Keys.isEnrolled(getApplication())
            Keys.sync(getApplication())
            if (!was && Keys.isEnrolled(getApplication())) {
                // The cached client was built without an identity; it cannot
                // acquire one retroactively.
                Net.reset()
            }
            val hello = api.hello()
            _notes.value = hello.notes
            // The design's boot line, and nothing more. "No onboarding, no
            // tour, no sample questions" — V does not introduce himself.
            _status.value = "${hello.notes} notes indexed"
            _fault.value = null
        } catch (e: Exception) {
            _status.value = "not reachable"
            _fault.value = reason(e)
        }
    }

    // ---- attachments out --------------------------------------------------
    /**
     * Upload what the picker handed back.
     *
     * The 25 MB cap is checked before the bytes are read, not after the upload
     * fails: every attachment becomes a git commit in a vault that gets cloned
     * and backed up, and the honest place to say so is here, before a minute
     * of radio has been spent on a screen recording.
     */
    fun attach(uri: android.net.Uri) = viewModelScope.launch {
        val app = getApplication<Application>()
        _busyFile.value = "attaching…"
        try {
            val picked = Files.read(app, uri, ATTACH_LIMIT)
            if (picked == null) {
                _fault.value = "that file is over the ${ATTACH_LIMIT / 1_000_000} MB " +
                    "attachment limit, or could not be read"
                return@launch
            }
            _pending.value = api.attach(picked.data, picked.name, picked.mime)
            _fault.value = null
        } catch (e: Exception) {
            _fault.value = reason(e)
        } finally {
            _busyFile.value = null
        }
    }

    fun dropPending() { _pending.value = null }

    // ---- a typed turn ----------------------------------------------------
    fun send(text: String) {
        val trimmed = text.trim()
        val file = _pending.value
        // A photo with no caption is a whole message, and the server takes it
        // as one. Only the empty-handed case is nothing to send.
        if ((trimmed.isEmpty() && file == null) || turnJob?.isActive == true) return
        _turns.value = _turns.value + Turn.user(
            if (file == null) trimmed
            else listOf("${V.NOTE} ${file.name}", trimmed)
                .filter { it.isNotBlank() }.joinToString("\n"))
        _pending.value = null
        turnJob = viewModelScope.launch {
            _ambient.value = Ambient.THINKING
            try {
                val turn = api.chat(trimmed, file?.rel ?: "", speak = true)
                _turns.value = _turns.value + turn
                _fault.value = null
                speak(turn)
            } catch (e: Exception) {
                _fault.value = reason(e)
                _ambient.value = Ambient.IDLE
            }
        }
    }

    // ---- attachments in ---------------------------------------------------
    /**
     * Fetch a file V offered and put it somewhere the phone can find it.
     *
     * Downloaded on demand rather than pushed with the turn: most files V
     * mentions are never opened, and four megabytes riding along with every
     * answer would make the fast local ones feel like the slow ones.
     */
    fun fetch(file: VFile, then: (String) -> Unit = {}) = viewModelScope.launch {
        val app = getApplication<Application>()
        _busyFile.value = file.rel
        try {
            val target = Files.scratch(app, file.name)
            java.io.FileOutputStream(target).use { api.download(file.rel, it) }
            val where = Files.publish(app, target, file.mime)
            _fault.value = null
            if (where != null) {
                then("saved to $where")
            } else {
                // Below Android 10 there is no permissionless Downloads
                // collection. The file exists; the share sheet is how it gets
                // anywhere the user chose, and saying so beats a silent no-op.
                Files.sendTo(app, target, file.mime)
                then("choose where to keep it")
            }
        } catch (e: Exception) {
            _fault.value = reason(e)
        } finally {
            _busyFile.value = null
        }
    }

    /** Open it in whatever handles that type, downloading first if needed. */
    fun openFile(file: VFile, note: (String) -> Unit = {}) = viewModelScope.launch {
        val app = getApplication<Application>()
        _busyFile.value = file.rel
        try {
            val target = Files.scratch(app, file.name)
            if (!target.exists() || target.length() != file.bytes) {
                java.io.FileOutputStream(target).use { api.download(file.rel, it) }
            }
            if (!Files.open(app, target, file.mime)) {
                // Nothing installed that opens a `.md`, which is the common
                // case on a phone. Not a failed download, and must not read
                // like one.
                note("nothing on this phone opens ${file.mime}")
            }
            _fault.value = null
        } catch (e: Exception) {
            _fault.value = reason(e)
        } finally {
            _busyFile.value = null
        }
    }

    fun press(button: Button) {
        if (turnJob?.isActive == true) return
        turnJob = viewModelScope.launch {
            _ambient.value = Ambient.THINKING
            try {
                val turn = api.act(button, speak = true)
                _turns.value = _turns.value + turn
                speak(turn)
            } catch (e: Exception) {
                _fault.value = reason(e)
                _ambient.value = Ambient.IDLE
            }
        }
    }

    // ---- a spoken turn ---------------------------------------------------
    /**
     * Hold-to-talk, by whichever route is fastest on this phone.
     *
     * Three of them, in order of what they cost him in waiting:
     *
     *  1. **The phone's own recognizer.** No upload, no Whisper, and the words
     *     appear as he says them. See `Stt` for why it is used only when the
     *     platform confirms the language pack is on the device.
     *  2. **Chunked upload.** The recorder cuts at silences and each piece is
     *     transcribed while he is still speaking, so the wait at the end is
     *     one pass over the tail rather than over everything.
     *  3. **The whole file**, which is what this always did.
     *
     * All three end in the same place, so the surface cannot tell which ran.
     *
     * ### The microphone
     *
     * The whole turn runs inside `Mic.withUi`, and that is a bug fix rather
     * than tidiness. `VService`'s gate holds the microphone open scoring
     * everything it hears against the wake word. Without this claim it went on
     * doing that *while he held the button*, heard the message he was dictating
     * into the composer, matched it — he is the enrolled speaker, at arm's
     * length, which is the easiest possible case for it — and opened a second
     * conversation on top of the one he was having. That conversation greeted
     * him with "Yes?", which is why every held message seemed to be answered
     * with it, and ended by calling `newSession`, which threw away the thread.
     *
     * Held for the answer and the speaking too, not only the recording: V's
     * own voice coming out of the speaker is also something the gate can hear.
     */
    fun startListening() {
        if (turnJob?.isActive == true) return
        turnJob = viewModelScope.launch {
            _ambient.value = Ambient.LISTENING
            _heard.value = ""
            try {
                Mic.withUi {
                    val turn = dictate() ?: return@withUi
                    if (turn.transcript.isNullOrBlank() && turn.text.isBlank()) {
                        return@withUi
                    }
                    // The transcript is shown as Parsa's own line before the
                    // answer — the design makes a wrong transcript a *state*,
                    // and a state you cannot see is one you cannot correct.
                    turn.transcript?.takeIf { it.isNotBlank() }?.let {
                        _turns.value = _turns.value + Turn.user(it)
                    }
                    if (turn.text.isNotBlank()) {
                        _turns.value = _turns.value + turn
                        _fault.value = null
                        speakBlocking(turn)
                    }
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                _fault.value = reason(e)
            } finally {
                _heard.value = ""
                java.util.Arrays.fill(recognizerBars, 0f)
                _levels.value = FloatArray(Recorder.BARS)
                _ambient.value = Ambient.IDLE
            }
        }
    }

    /** Whichever of the three routes this phone can run. Null means nothing
     *  was said, or the microphone could not be opened — both of which are
     *  ordinary and neither of which is an error dialog. */
    private suspend fun dictate(): Turn? {
        if (stt.usable()) {
            val text = stt.listen(
                onPartial = { _heard.value = it },
                onLevel = { level -> _levelsFromRecognizer(level) },
                register = { stopper -> onDeviceStop = stopper })
            onDeviceStop = null
            if (text != null) {
                // "" is a button brushed, not a failure. Nothing to send.
                if (text.isBlank()) return null
                _ambient.value = Ambient.THINKING
                _heard.value = ""
                // Text, not audio: the recognizer already did the only job the
                // upload existed for. `transcript` is filled in here because
                // the server never saw a voice message and so cannot echo one.
                return api.chat(text, speak = true).copy(transcript = text)
            }
            // Null is a broken recognizer, not silence — fall through and
            // record properly rather than dropping what he said.
        }

        // `untilSilence = false`: on this surface the button is held, so the
        // finger is the endpoint detector and a better one than any threshold.
        // The handsfree path is the one that has to guess.
        val heardIt = dictation.listen(
            untilSilence = false,
            onPartial = { _heard.value = it },
            onThinking = { _ambient.value = Ambient.THINKING })
        heardIt.turn?.let { return it }
        val wav = heardIt.wav ?: return null
        _ambient.value = Ambient.THINKING
        return api.voice(wav, speak = true)
    }

    /** Set by `Stt.listen` while the phone's own recognizer owns the mic, so
     *  the button's release can end that turn as well as a recorded one. */
    private var onDeviceStop: Stt.Stopper? = null

    /** One RMS reading from the phone's recognizer, shifted into the bar
     *  window the same way `Recorder` shifts its own — so `Waveform` cannot
     *  tell which path is feeding it. */
    private fun _levelsFromRecognizer(level: Float) {
        val bars = recognizerBars
        System.arraycopy(bars, 1, bars, 0, bars.size - 1)
        bars[bars.size - 1] = level
        _levels.value = bars.copyOf()
    }

    private val recognizerBars = FloatArray(Recorder.BARS)

    fun stopListening() {
        onDeviceStop?.stop()
        recorder.requestStop()
    }

    private fun speak(turn: Turn) {
        speakJob?.cancel()
        if (turn.spoken.ifBlank { turn.text }.isBlank()) {
            _ambient.value = Ambient.IDLE
            return
        }
        speakJob = viewModelScope.launch { speakBlocking(turn) }
    }

    /**
     * Speak it and wait, rather than launching and returning.
     *
     * The spoken path needs this: `startListening` holds the microphone for
     * the length of the turn, and a `speak` that returned immediately would
     * drop that claim while V was still talking — handing the gate back a
     * microphone pointed at V's own voice.
     */
    private suspend fun speakBlocking(turn: Turn) {
        if (turn.spoken.ifBlank { turn.text }.isBlank()) return
        _ambient.value = Ambient.SPEAKING
        try {
            speech.play(turn)
        } catch (e: kotlinx.coroutines.CancellationException) {
            throw e
        } catch (e: Exception) {
            // No voice is not a failed turn. The answer is already read.
        } finally {
            _ambient.value = Ambient.IDLE
        }
    }

    fun hush() {
        speakJob?.cancel()
        player.stop()
        _ambient.value = Ambient.IDLE
    }

    fun newSession() = viewModelScope.launch {
        try {
            api.newSession()
            _turns.value = emptyList()
        } catch (e: Exception) {
            _fault.value = reason(e)
        }
    }

    fun clearFault() { _fault.value = null }

    companion object {
        /** `files.ATTACH_LIMIT` on the server, repeated here so the refusal
         *  happens before the radio is woken. The two are allowed to drift
         *  only in the safe direction: the server is the one that enforces. */
        const val ATTACH_LIMIT = 25L * 1024 * 1024
    }

    override fun onCleared() {
        player.stop()
        super.onCleared()
    }

    /**
     * What to put on screen when something failed.
     *
     * A revoked device gets its own sentence rather than "network error",
     * because it is the one failure Parsa caused and can fix, and the one that
     * otherwise looks exactly like the laptop being asleep.
     */
    private fun reason(e: Exception): String = when {
        // Before a certificate arrives there is no identity to present, so
        // this is not a network problem and must not read like one. The
        // instruction is the whole message: the key is already generated and
        // waiting to be signed.
        e is Keys.NotEnrolled ->
            "not enrolled yet — run `cli phone enrol pixel` on the laptop " +
            "with this phone plugged in"
        e is ApiException && e.status == 403 -> e.message ?: "this device is not enrolled"
        e is ApiException -> e.message ?: "V refused that (${e.status})"
        else -> "can't reach V — is the laptop awake and `cli serve` running?"
    }
}
