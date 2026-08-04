package com.tonghui.pms;

import android.app.DownloadManager;
import android.content.Context;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.webkit.URLUtil;
import android.webkit.WebView;
import android.widget.Toast;

import androidx.activity.OnBackPressedCallback;
import androidx.core.view.WindowCompat;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.PluginHandle;

/**
 * 同辉项目管理 · 手机端。
 *
 * <p>前端资源<b>打在包里、从本地加载</b>（Capacitor），不再每次开 APP 都去服务器取页面。
 * 换前端走热更新（{@link PmsUpdater}），不用重发 APK。
 *
 * <p>这个类只做四件 Capacitor 默认没有、而少了就出事的事：
 * <ol>
 *   <li>启动前决定这次加载哪个前端包（必须早于 super.onCreate）</li>
 *   <li>试用超时没人报平安 → 当场回滚，别让用户对着白屏</li>
 *   <li>返回键在页面内后退，不是一按就退出</li>
 *   <li>附件下载交给系统下载器；WebView 自己存不了</li>
 * </ol>
 */
public class MainActivity extends BridgeActivity {

    /** 新前端包最多试用这么久；到点还没报平安就当它坏了 */
    private static final long TRIAL_TIMEOUT_MS = 10_000L;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private Runnable trialTimeout;
    private boolean webReady = false;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        // ⚠️ 两件事都必须在 super.onCreate 之前：
        //   · registerPlugin 往 bridgeBuilder 里加插件，而 Bridge 在 super.onCreate 末尾才 create
        //   · beforeBridgeLoad 写的正是 Bridge 构造时要读的偏好（这次加载哪个前端包）
        //   晚一步都不会报错，只会「这次没生效、下次才生效」，查起来极费解。
        registerPlugin(PmsUpdaterPlugin.class);
        registerPlugin(PmsSpeechPlugin.class);
        PmsUpdater.beforeBridgeLoad(this);

        super.onCreate(savedInstanceState);

        // 铺满到刘海和 Home 条下面。不这么做系统会替我们留白，
        // WebView 拿到的 env(safe-area-inset-*) 全是 0 —— H5 里那套安全区适配等于白写。
        WindowCompat.setDecorFitsSystemWindows(getWindow(), false);

        setupDownloads();
        setupBackButton();
        armTrialTimeout();
    }

    /** 前端报平安。撤掉超时，顺便查一次更新（这时首屏已经出来，不跟它抢带宽）。 */
    void onWebReady() {
        ui.post(() -> {
            webReady = true;
            if (trialTimeout != null) {
                ui.removeCallbacks(trialTimeout);
                trialTimeout = null;
            }
            if (getBridge() == null) return;
            PluginHandle h = getBridge().getPlugin("PmsUpdater");
            if (h != null && h.getInstance() instanceof PmsUpdaterPlugin) {
                ((PmsUpdaterPlugin) h.getInstance()).checkInBackground();
            }
        });
    }

    /**
     * 回滚到上一个能用的前端包并立刻重载。
     *
     * <p>为什么当场回滚而不是等下次启动：用户此刻正对着白屏。
     * 让他重启 APP 才恢复，等于把一个我们自己能解决的问题丢给他。
     */
    void rollbackNow(String reason) {
        ui.post(() -> {
            if (webReady) return;                       // 已经起来了就不算启动失败
            String target = PmsUpdater.markFailed(this, reason);
            if ("".equals(target)) return;              // 跑的是内置包，没有可退的
            Toast.makeText(this, "新版本没能启动，已退回上一版", Toast.LENGTH_LONG).show();
            if (getBridge() == null) return;
            if (target == null) getBridge().setServerAssetPath("public");
            else getBridge().setServerBasePath(target);
        });
    }

    private void armTrialTimeout() {
        if (PmsUpdater.activeVersion(this) == null) return;   // 内置包，不需要试用期
        trialTimeout = () -> {
            trialTimeout = null;
            rollbackNow("启动 " + (TRIAL_TIMEOUT_MS / 1000) + " 秒内没有报平安");
        };
        ui.postDelayed(trialTimeout, TRIAL_TIMEOUT_MS);
    }

    /** 附件下载交给系统下载器 —— WebView 自己没法把文件落到磁盘 */
    private void setupDownloads() {
        WebView web = getBridge() == null ? null : getBridge().getWebView();
        if (web == null) return;
        web.setDownloadListener((url, userAgent, contentDisposition, mimeType, size) -> {
            try {
                DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
                req.setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(
                        android.os.Environment.DIRECTORY_DOWNLOADS,
                        URLUtil.guessFileName(url, contentDisposition, mimeType));
                ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(req);
                Toast.makeText(this, "开始下载，完成后在「下载」里找", Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(this, "下载失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
            }
        });
    }

    /**
     * 返回键先在页面内后退。
     *
     * <p>⚠️ Capacitor 的 BridgeActivity <b>没有</b>处理硬件返回键（它把这件事交给
     * &#64;capacitor/app 插件，而我们没装）。不接的话按一下就直接退出 APP ——
     * 这正是旧壳里专门处理过、迁移时最容易丢掉的一条。
     */
    private void setupBackButton() {
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                WebView web = getBridge() == null ? null : getBridge().getWebView();
                if (web != null && web.canGoBack()) web.goBack();
                else finish();
            }
        });
    }
}
