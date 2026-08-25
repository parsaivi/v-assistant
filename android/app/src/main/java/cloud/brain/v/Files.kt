package cloud.brain.v

import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.provider.OpenableColumns
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

/**
 * Getting a file off this phone and onto it, either direction.
 *
 * Both halves are more awkward than they look, and for the same reason: an
 * Android app does not have a filesystem it shares with the user. A picked
 * file arrives as a `content://` URI it can read once, and a file it wants to
 * hand back has to be published somewhere the rest of the system can see.
 * Everything here is that translation and nothing else — the vault side of it
 * lives in `core/files.py`.
 */
object Files {

    /** What a picked URI actually is: bytes, a name, and a type. */
    data class Picked(val data: ByteArray, val name: String, val mime: String)

    /** Read once, into memory, because `/v1/attach` posts a whole body and the
     *  server caps that at 25 MB anyway. [limit] is that cap, checked here so
     *  a 400 MB video fails on this side of the radio rather than after
     *  uploading itself. */
    suspend fun read(context: Context, uri: Uri, limit: Long): Picked? =
        withContext(Dispatchers.IO) {
            val resolver = context.contentResolver
            val name = displayName(context, uri) ?: "file"
            val mime = resolver.getType(uri) ?: guessMime(name)
            val size = sizeOf(context, uri)
            if (size != null && size > limit) return@withContext null
            val bytes = try {
                resolver.openInputStream(uri)?.use { it.readBytes() }
            } catch (e: Exception) {
                null
            } ?: return@withContext null
            if (bytes.size > limit) return@withContext null
            Picked(bytes, name, mime)
        }

    /**
     * The name the user knows the file by.
     *
     * `OpenableColumns.DISPLAY_NAME` and not the URI's last path segment: a
     * document URI's last segment is commonly `msf:1000000042`, and filing
     * that in the vault would produce an attachment nobody can identify.
     */
    private fun displayName(context: Context, uri: Uri): String? {
        if (uri.scheme == "file") return uri.lastPathSegment
        return try {
            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME),
                                          null, null, null)?.use { c ->
                if (c.moveToFirst() && !c.isNull(0)) c.getString(0) else null
            }
        } catch (e: Exception) {
            null
        }
    }

    private fun sizeOf(context: Context, uri: Uri): Long? = try {
        context.contentResolver.query(uri, arrayOf(OpenableColumns.SIZE),
                                      null, null, null)?.use { c ->
            if (c.moveToFirst() && !c.isNull(0)) c.getLong(0) else null
        }
    } catch (e: Exception) {
        null
    }

    private fun guessMime(name: String): String = when (name.substringAfterLast('.', "")
        .lowercase()) {
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "gif" -> "image/gif"
        "webp" -> "image/webp"
        "pdf" -> "application/pdf"
        "txt", "md" -> "text/plain"
        "mp4" -> "video/mp4"
        "mp3" -> "audio/mpeg"
        else -> "application/octet-stream"
    }

    // ---- the other direction ---------------------------------------------
    /** Where a download is assembled before it is published. App-private, so
     *  nothing here needs a storage permission. */
    fun scratch(context: Context, name: String): File {
        val dir = File(context.filesDir, "downloads").apply { mkdirs() }
        return File(dir, name.ifBlank { "file" })
    }

    /**
     * Put a downloaded file where the user can find it again.
     *
     * `MediaStore.Downloads` from Android 10, which is the whole point of it:
     * an app may write to the shared Downloads collection with no permission
     * at all, and the file survives this app being uninstalled. Below 10 there
     * is no such collection, and reaching the public directory needs
     * `WRITE_EXTERNAL_STORAGE` — a permission this app should not hold for a
     * feature this size — so those phones get the share sheet instead, and are
     * told so rather than being left to wonder where the file went.
     *
     * Returns the user-facing location, or null if it could not be published.
     */
    suspend fun publish(context: Context, file: File, mime: String): String? =
        withContext(Dispatchers.IO) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return@withContext null
            try {
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, file.name)
                    put(MediaStore.Downloads.MIME_TYPE, mime)
                    put(MediaStore.Downloads.IS_PENDING, 1)
                }
                val resolver = context.contentResolver
                val target = resolver.insert(
                    MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                    ?: return@withContext null
                resolver.openOutputStream(target)?.use { out ->
                    file.inputStream().use { it.copyTo(out) }
                } ?: return@withContext null
                values.clear()
                values.put(MediaStore.Downloads.IS_PENDING, 0)
                resolver.update(target, values, null, null)
                Environment.DIRECTORY_DOWNLOADS + "/" + file.name
            } catch (e: Exception) {
                null
            }
        }

    /** A content URI other apps may read, for "open" and "share". */
    fun share(context: Context, file: File): Uri =
        FileProvider.getUriForFile(context, "${context.packageName}.files", file)

    /** Hand the file to whatever can open it. Returns false when nothing can —
     *  a `.md` on a phone with no text editor is the common case, and it must
     *  not look like the download failed. */
    fun open(context: Context, file: File, mime: String): Boolean {
        val intent = Intent(Intent.ACTION_VIEW)
            .setDataAndType(share(context, file), mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or
                      Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            true
        } catch (e: Exception) {
            false
        }
    }

    fun sendTo(context: Context, file: File, mime: String) {
        val intent = Intent(Intent.ACTION_SEND)
            .setType(mime)
            .putExtra(Intent.EXTRA_STREAM, share(context, file))
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        try {
            context.startActivity(Intent.createChooser(intent, "Save ${file.name}")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) {
            // Nothing installed that accepts it. The file is still on disk.
        }
    }
}
