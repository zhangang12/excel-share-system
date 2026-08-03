package com.tonghui.pms

import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.*
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout

/**
 * 同辉项目管理 · 手机端
 *
 * 就是把服务器上的 H5（/h5/）包进一个 WebView，**页面资源全部走服务器**：
 * 改一行前端不用重新发 APP，手机上看到的和网页端永远一致。
 *
 * 为什么不用 Capacitor/Cordova：这个 APP 干的事就是「打开一个网址」，
 * 上一整套框架只会多出 npm 依赖链和几 MB 体积，没有任何收益。
 *
 * ⚠️ 服务器目前只有 HTTP（没有 HTTPS），所以清单里开了 usesCleartextTraffic。
 *    等服务器上了 HTTPS，把 network_security_config.xml 里的域名去掉即可。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var refresh: SwipeRefreshLayout
    private lateinit var offline: View
    private var lastFailed = false

    /** 选文件（反馈截图之类）用的回调，onShowFileChooser 里存下来 */
    private var filePathCallback: ValueCallback<Array<Uri>>? = null

    private val filePicker = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val cb = filePathCallback ?: return@registerForActivityResult
        filePathCallback = null
        cb.onReceiveValue(
            WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
        )
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, true)
        setContentView(R.layout.activity_main)

        web = findViewById(R.id.web)
        refresh = findViewById(R.id.refresh)
        offline = findViewById(R.id.offline)

        web.settings.apply {
            javaScriptEnabled = true
            // ⚠️ 必须开：H5 把登录令牌存在 localStorage，不开的话每次都要重新登录
            domStorageEnabled = true
            databaseEnabled = true
            loadWithOverviewMode = true
            useWideViewPort = true
            // 手机上不需要缩放，双指缩放会让布局错乱
            builtInZoomControls = false
            displayZoomControls = false
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
            // 带上标识，服务端的桌面/移动统计能分得清
            userAgentString = "$userAgentString PMSMobile/${BuildConfig.VERSION_NAME}"
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true)

        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                v: WebView?, req: WebResourceRequest?
            ): Boolean {
                val url = req?.url ?: return false
                // 站内跳转留在 APP 里；外站交给系统浏览器，别把人带出业务范围
                if (url.host == Uri.parse(APP_URL).host) return false
                return try {
                    startActivity(android.content.Intent(android.content.Intent.ACTION_VIEW, url))
                    true
                } catch (e: Exception) {
                    false
                }
            }

            override fun onPageFinished(v: WebView?, url: String?) {
                refresh.isRefreshing = false
                // 只有主框架真失败过才显示离线页；子资源失败（图标之类）不算
                if (!lastFailed) showWeb()
            }

            override fun onReceivedError(
                v: WebView?, req: WebResourceRequest?, err: WebResourceError?
            ) {
                if (req?.isForMainFrame != true) return
                lastFailed = true
                refresh.isRefreshing = false
                showOffline()
            }
        }

        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(
                v: WebView?, cb: ValueCallback<Array<Uri>>?,
                params: FileChooserParams?
            ): Boolean {
                filePathCallback?.onReceiveValue(null)   // 上一次没走完的先取消，防泄漏
                filePathCallback = cb
                return try {
                    filePicker.launch(params?.createIntent())
                    true
                } catch (e: Exception) {
                    filePathCallback = null
                    false
                }
            }
        }

        // 附件下载交给系统下载器，WebView 自己存不了
        web.setDownloadListener { url, _, contentDisposition, mimeType, _ ->
            try {
                val req = DownloadManager.Request(Uri.parse(url)).apply {
                    setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    setDestinationInExternalPublicDir(
                        android.os.Environment.DIRECTORY_DOWNLOADS,
                        URLUtil.guessFileName(url, contentDisposition, mimeType))
                }
                (getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
                Toast.makeText(this, "开始下载，完成后在「下载」里找", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                Toast.makeText(this, "下载失败：${e.message}", Toast.LENGTH_LONG).show()
            }
        }

        refresh.setOnRefreshListener { load() }
        findViewById<View>(R.id.retry).setOnClickListener { load() }

        // 返回键：先在 H5 里后退，退到头了再退出 APP
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (web.canGoBack()) web.goBack() else finish()
            }
        })

        load()
    }

    private fun load() {
        lastFailed = false
        showWeb()
        web.loadUrl(APP_URL)
    }

    private fun showWeb() {
        offline.visibility = View.GONE
        web.visibility = View.VISIBLE
    }

    private fun showOffline() {
        web.visibility = View.GONE
        offline.visibility = View.VISIBLE
    }

    override fun onDestroy() {
        filePathCallback?.onReceiveValue(null)
        filePathCallback = null
        super.onDestroy()
    }

    companion object {
        /** H5 入口。**页面在服务器上**，APP 只是个壳。 */
        const val APP_URL = "http://8.141.123.141/h5/"
    }
}
