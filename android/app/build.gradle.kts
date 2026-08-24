plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "cloud.brain.v"
    compileSdk = 35

    defaultConfig {
        applicationId = "cloud.brain.v"
        // 26 rather than lower: the foreground service, the notification
        // channel and the bluetooth ACL broadcast exemption all begin at O,
        // and every phone this will ever be installed on is well past it.
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"

        // arm64 only. The onnxruntime AAR ships four ABIs at ~7MB each and
        // every phone this will be installed on is arm64; carrying x86 and
        // armeabi would triple the APK for no device that exists here.
        ndk { abiFilters += "arm64-v8a" }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"),
                          "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }

    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
        // The .onnx weights are already compressed; zipping them again costs
        // build time and, worse, forces a full decompress-to-heap on load.
        // Left uncompressed they are mmap'd straight out of the APK.
        androidResources { noCompress += listOf("onnx", "f32") }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-service:2.8.7")
    implementation("androidx.activity:activity-compose:1.9.3")

    val compose = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(compose)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")

    // OkHttp for the mutual-TLS client and the SSE stream. No Retrofit, no
    // Moshi, no kotlinx-serialization: there are eight endpoints and `org.json`
    // is in the platform — the same reasoning that keeps `google-api-python-
    // client` and `python-telegram-bot` out of the server.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.squareup.okhttp3:okhttp-sse:4.12.0")

    // The wake gate. Three small models run on every frame of audio that
    // reaches the phone, and they have to run *on* the phone: the whole point
    // of the gate is that nothing crosses the wire until it has passed. There
    // is no way to execute an ONNX graph on Android without a runtime, so this
    // is the one dependency the feature cannot be built without.
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.20.0")

    // Voiceprint storage. A speaker centroid is biometric data; it is kept
    // Keystore-wrapped rather than in plain SharedPreferences.
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // The feature front-end is checked against golden vectors from the Python
    // reference on the JVM, so the riskiest code in the gate does not need a
    // phone to be verified.
    testImplementation("junit:junit:4.13.2")
}
