plugins {
    id("com.android.application")
}

fun providerValue(name: String, envName: String, defaultValue: String) =
    providers.gradleProperty(name)
        .orElse(providers.environmentVariable(envName))
        .orElse(defaultValue)

fun buildConfigString(value: String): String {
    val escaped = value
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
    return "\"$escaped\""
}

val defaultApiUrl = providerValue(
    "statisticApiUrl",
    "STATISTIC_API_URL",
    "http://109.196.100.116:8091/api/statistic",
)
val defaultApiToken = providerValue("statisticApiToken", "STATISTIC_API_TOKEN", "")

android {
    namespace = "com.megaskidka.poststats"
    compileSdk = 36

    buildFeatures {
        buildConfig = true
    }

    defaultConfig {
        applicationId = "com.megaskidka.poststats"
        minSdk = 23
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        buildConfigField("String", "DEFAULT_API_URL", buildConfigString(defaultApiUrl.get()))
        buildConfigField("String", "DEFAULT_API_TOKEN", buildConfigString(defaultApiToken.get()))
    }
}

val copyDebugApk by tasks.registering {
    val debugApk = layout.buildDirectory.file("outputs/apk/debug/app-debug.apk")
    val targetApk = layout.projectDirectory.file("../post-statistics.apk")

    inputs.file(debugApk)
    outputs.file(targetApk)

    doLast {
        debugApk.get().asFile.copyTo(targetApk.asFile, overwrite = true)
    }
}

tasks.matching { it.name == "assembleDebug" }.configureEach {
    finalizedBy(copyDebugApk)
}
