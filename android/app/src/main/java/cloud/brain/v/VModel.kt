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

    val levels: StateFlow<FloatArray> = recorder.levels

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

    init { refresh() }

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
                val turn = api.chat(trimmed, file?.rel ?: "")
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
                val turn = api.act(button)
                _turns.value = _turns.value + turn
                speak(turn)
            } catch (e: Exception) {
                _fault.value = reason(e)
                _ambient.value = Ambient.IDLE
            }
        }
    }

    // ---- a spoken turn ---------------------------------------------------
    fun startListening() {
        if (turnJob?.isActive == true) return
        turnJob = viewModelScope.launch {
            _ambient.value = Ambient.LISTENING
            // `untilSilence = false`: on this surface the button is held, so
            // the finger is the endpoint detector and a better one than any
            // threshold. The handsfree path is the one that has to guess.
            val wav = recorder.record(untilSilence = false)
            if (wav == null) {
                _ambient.value = Ambient.IDLE
                return@launch
            }
            _ambient.value = Ambient.THINKING
            try {
                val turn = api.voice(wav)
                // The transcript is shown as Parsa's own line before the answer
                // — the design makes a wrong transcript a *state*, and a state
                // you cannot see is one you cannot correct.
                turn.transcript?.takeIf { it.isNotBlank() }?.let {
                    _turns.value = _turns.value + Turn.user(it)
                }
                if (turn.text.isNotBlank()) {
                    _turns.value = _turns.value + turn
                    speak(turn)
                } else {
                    _ambient.value = Ambient.IDLE
                }
            } catch (e: Exception) {
                _fault.value = reason(e)
                _ambient.value = Ambient.IDLE
            }
        }
    }

    fun stopListening() = recorder.requestStop()

    private fun speak(turn: Turn) {
        speakJob?.cancel()
        val words = turn.spoken.ifBlank { turn.text }
        if (words.isBlank()) {
            _ambient.value = Ambient.IDLE
            return
        }
        speakJob = viewModelScope.launch {
            _ambient.value = Ambient.SPEAKING
            try {
                player.play(api.speak(words))
            } catch (e: Exception) {
                // No voice is not a failed turn. The answer is already read.
            } finally {
                _ambient.value = Ambient.IDLE
            }
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
