package cloud.brain.v

import android.Manifest
import android.app.Application
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel

/** Nothing here but the window, the permissions the window needs, and the
 *  question of which of the three surfaces is the right one to show. */
class MainActivity : ComponentActivity() {

    companion object {
        /** Set by `VService` when the gate fired, so the window comes up as the
         *  conversation rather than as the chat log. */
        const val EXTRA_VOICE = "voice"
    }

    private val permissions = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()) { granted ->
        // The gate cannot start without RECORD_AUDIO, and the service already
        // decided it had none before this dialog was answered. Nudging it is
        // the difference between listening now and listening after a reboot.
        if (granted[Manifest.permission.RECORD_AUDIO] == true) VService.start(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        showOverLockScreen()
        ask()
        // Before anything tries to open a socket: generate the keypair in the
        // secure element if this is a fresh install, and publish its public
        // half where `cli phone enrol` can fetch it. Doing it here means the
        // key exists to be signed after the very first launch, with nothing
        // for anyone to remember to press.
        Keys.sync(this)
        VService.start(this)

        setContent {
            val prints = remember { Voiceprint(this) }
            var enrolled by remember { mutableStateOf(prints.isEnrolled) }
            val phase by VoiceSession.phase.collectAsState()
            var dismissed by remember { mutableStateOf(false) }

            val wantsVoice = (intent?.getBooleanExtra(EXTRA_VOICE, false) == true ||
                              phase != VoiceSession.Phase.IDLE) && !dismissed

            // Calibration opens by itself while the gate is still in shadow
            // mode, because a gate that scores everything and acts on nothing
            // is not finished being set up — and until this screen existed
            // there was no way to finish. Once it is live, it is somewhere the
            // user goes deliberately.
            var calibrating by remember { mutableStateOf(false) }
            val needsCalibration = enrolled && prints.shadowMode && !wantsVoice

            when {
                // Enrollment first, always. Nothing else about the gate works
                // until it knows what it is listening for.
                !enrolled -> EnrollScreen(onDone = {
                    enrolled = true
                    requestOverlay()
                    VService.start(this)
                })
                wantsVoice -> VoiceScreen(onDone = { dismissed = true })
                calibrating || needsCalibration -> CalibrateScreen(
                    onDone = { calibrating = false },
                    onReenroll = { enrolled = false; calibrating = false })
                else -> {
                    val model: VModel = viewModel()
                    ChatScreen(model, onCalibrate = { calibrating = true })
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        // singleTask, so a wake arriving while the window is already up lands
        // here rather than in onCreate. Re-setting it is what lets the
        // `EXTRA_VOICE` read above see the new one.
        setIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        // The laptop sleeps, the lease changes, the service reconnects. Asking
        // again on every return costs one request and is how the header stops
        // saying "not reachable" the moment it stops being true.
        VService.start(this)
    }

    /** A wake at 2am should not need the phone unlocked to see what is
     *  listening — and, more to the point, to be able to stop it. */
    private fun showOverLockScreen() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
    }

    /**
     * The permissions, asked together, once.
     *
     * No rationale screen and no onboarding — the design says "no onboarding,
     * no tour, no sample questions", and a permission dialog with a lecture in
     * front of it is a tour with one stop.
     *
     * The bluetooth permission that used to be here is gone along with the
     * trigger that needed it.
     */
    private fun ask() {
        val wanted = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            wanted += Manifest.permission.POST_NOTIFICATIONS
        }
        val missing = wanted.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) permissions.launch(missing.toTypedArray())
    }

    /**
     * Send him to the one Settings toggle that lets a wake put itself on screen.
     *
     * `SYSTEM_ALERT_WINDOW` is not a runtime permission — there is no dialog to
     * launch, only a Settings page — and it is what exempts the service from
     * the background-activity-start restriction that has applied since Android
     * 10. Asked for once, after enrollment, because before that the gate cannot
     * fire and the request would be a tour stop with nothing behind it.
     */
    private fun requestOverlay() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(this)) {
            try {
                startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                     Uri.parse("package:$packageName")))
            } catch (e: Exception) {
                // Some ROMs hide the page. The gate still works; the
                // conversation just arrives as a notification instead.
            }
        }
    }
}

/** Kept so the manifest has a stable application class to name; the service
 *  and the client are both lazy, so there is nothing to do at start-up. */
class VApp : Application()
