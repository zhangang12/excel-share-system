plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.tonghui.pms"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.tonghui.pms"
        // minSdk 24 = Android 7。再低的机器 WebView 版本太老，H5 里的
        // CSS 变量、fetch/ReadableStream（流式输出用的）都跑不起来。
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"
    }

    buildFeatures { buildConfig = true }

    buildTypes {
        release {
            // 壳里没什么可混淆的，关掉省得 WebView 回调被裁掉
            isMinifyEnabled = false
            // ⚠️ 用 debug 签名出 release 包：内部分发用，不上应用商店。
            //    要上商店得换成正式签名（keystore 不能进仓库）。
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.2")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
}
