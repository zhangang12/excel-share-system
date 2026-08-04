package com.tonghui.pms;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.util.Base64;
import android.util.Log;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.KeyFactory;
import java.security.MessageDigest;
import java.security.PublicKey;
import java.security.Signature;
import java.security.spec.X509EncodedKeySpec;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * 前端包热更新 —— 跟桌面客户端一个路子：服务器放清单，APP 自己取、自己验、自己换。
 *
 * <p><b>换包靠的是 Capacitor 自带的机制</b>：本地服务器从哪个目录取文件是可切换的
 * （{@code CapWebViewSettings/serverBasePath}），Bridge 启动时会读这个偏好。
 * 所以「换前端」= 换一个目录，不用重装 APK。
 *
 * <h3>为什么要有试用期与回滚</h3>
 * 桌面客户端 1.0.30/1.0.31 的教训：包发出去了、构建全绿、上传也绿，
 * 装上去打不开 —— <b>没有任何一环能发现「装上不能用」</b>，只能等用户来喊。
 * 这里的做法是：新包先标成「试用」，前端起来后必须回一声
 * （{@code notifyReady}）才转正；收不到就<b>拉黑这个版本并退回上一个好包</b>。
 * 白屏最多持续到超时（10 秒）或下次启动。
 *
 * <h3>为什么要验签</h3>
 * 服务器目前是明文 HTTP。不验签的话，同一个 WiFi 下任何人都能把任意 JS
 * 塞进这个 APP —— 而它手里有用户的登录令牌，能审批付款。
 * 所以：公钥打进 APK，私钥只在发版机器上；<b>拿不到公钥就一律不更新</b>（失败即拒绝，
 * 不是「验不了就放行」）。
 *
 * <h3>状态</h3>
 * <pre>
 *   内置包  ── 下载校验通过 ─→ pending ── 下次启动 ─→ trial ── 收到平安 ─→ lastGood
 *                                              └── 超时/报错 ─→ 拉黑 + 退回 lastGood
 * </pre>
 */
public final class PmsUpdater {

    private static final String TAG = "PmsUpdater";

    /** 我们自己的状态；Capacitor 的偏好在另一个文件里，互不干扰 */
    private static final String PREFS = "PmsUpdaterState";
    /** Capacitor 读这个偏好决定本地服务器从哪个目录取文件 —— 换包的开关就是它 */
    private static final String CAP_PREFS = "CapWebViewSettings";
    private static final String CAP_SERVER_PATH = "serverBasePath";

    private static final String K_PENDING = "pendingVersion";
    private static final String K_TRIAL = "trialVersion";
    private static final String K_LAST_GOOD = "lastGoodVersion";
    private static final String K_BAD = "badVersions";
    private static final String K_BINARY_CODE = "binaryVersionCode";
    private static final String K_LAST_CHECK = "lastCheckAt";

    /** 前端包解压到这里，一个版本一个目录 */
    private static final String BUNDLE_DIR = "pms-web";
    /** 验签公钥。**没有这个文件就完全不更新** */
    private static final String PUBLIC_KEY_ASSET = "ota_public_key.pem";

    /** 单个前端包的体积上限；防的是「服务器被换成一个无限流」把手机磁盘写满 */
    private static final long MAX_BUNDLE_BYTES = 40L * 1024 * 1024;
    /** 解压后总大小上限，zip 炸弹防护 */
    private static final long MAX_UNPACKED_BYTES = 120L * 1024 * 1024;

    private PmsUpdater() {}

    // ────────────────────────── 启动前决定加载哪个包 ──────────────────────────

    /**
     * <b>必须在 {@code super.onCreate()} 之前调用。</b>
     * Bridge 是在 super.onCreate 里构造的，构造时就把 serverBasePath 读走了 ——
     * 晚一步写偏好，这次启动就还是加载旧包，要等下一次才生效（查起来极其费解）。
     */
    public static void beforeBridgeLoad(Activity activity) {
        SharedPreferences sp = activity.getSharedPreferences(PREFS, Context.MODE_PRIVATE);

        // ① 换了 APK：新壳自带新的内置前端包，之前热更新下来的一律作废。
        //    不清的话会出现「装了新 APP 却还跑着老前端」，而且老前端可能要的正是老壳。
        String code = binaryVersionCode(activity);
        if (!code.equals(sp.getString(K_BINARY_CODE, null))) {
            Log.i(TAG, "检测到新 APK（" + code + "），清空热更新状态，回到内置包");
            wipeBundles(activity);
            sp.edit().clear().putString(K_BINARY_CODE, code).apply();
            setServerBasePath(activity, null);
            return;
        }

        // ② 上次启动是在试用，但一直没收到「平安」→ 那个包是坏的，拉黑并退回
        String trial = sp.getString(K_TRIAL, null);
        if (trial != null) {
            Log.w(TAG, "上次试用的前端包 " + trial + " 没报平安，拉黑并回滚");
            blacklist(sp, trial);
            deleteDir(bundleDir(activity, trial));
            sp.edit().remove(K_TRIAL).apply();
            setServerBasePath(activity, goodDirPath(activity, sp));
            return;
        }

        // ③ 有下载好的新包 → 这次启动试用它
        String pending = sp.getString(K_PENDING, null);
        if (pending != null && bundleDir(activity, pending).isDirectory()) {
            Log.i(TAG, "试用新前端包 " + pending);
            sp.edit().remove(K_PENDING).putString(K_TRIAL, pending).apply();
            setServerBasePath(activity, bundleDir(activity, pending).getAbsolutePath());
            return;
        }

        // ④ 平稳启动：继续用上一个好包（没有就是内置包）
        setServerBasePath(activity, goodDirPath(activity, sp));
    }

    /** 前端报平安：把试用包转正 */
    public static void markReady(Context ctx) {
        SharedPreferences sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String trial = sp.getString(K_TRIAL, null);
        if (trial == null) return;   // 内置包起来的，没什么要转正的
        Log.i(TAG, "前端包 " + trial + " 报平安，转正");
        sp.edit().remove(K_TRIAL).putString(K_LAST_GOOD, trial).apply();
        pruneOldBundles(ctx, trial);
    }

    /**
     * 前端起不来。拉黑当前试用包并退回上一个好包。
     *
     * @return 需要退回时返回目标目录（null 表示退回内置包）；不需要动时返回空字符串
     */
    public static String markFailed(Context ctx, String reason) {
        SharedPreferences sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String trial = sp.getString(K_TRIAL, null);
        if (trial == null) return "";
        Log.w(TAG, "前端包 " + trial + " 启动失败（" + reason + "），回滚");
        blacklist(sp, trial);
        deleteDir(bundleDir(ctx, trial));
        sp.edit().remove(K_TRIAL).apply();
        String good = goodDirPath(ctx, sp);
        setServerBasePath(ctx, good);
        return good;
    }

    // ────────────────────────── 查更新 / 下载 / 校验 ──────────────────────────

    public static class Result {
        public final String status;    // ok / none / shell-too-old / error
        public final String version;
        public final String message;
        Result(String status, String version, String message) {
            this.status = status; this.version = version; this.message = message;
        }
    }

    /**
     * 拉清单 → 比版本 → 下载 → 验签 → 解压 → 标记 pending（下次启动生效）。
     * 全程同步，调用方负责放到后台线程。任何一步失败都只是「这次没更新成」，
     * 不动当前正在跑的包。
     */
    public static Result check(Context ctx, String manifestUrl, String shellVersion) {
        SharedPreferences sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        try {
            JSONObject m = new JSONObject(new String(fetch(manifestUrl, 256 * 1024), StandardCharsets.UTF_8));
            String version = m.optString("version", "");
            String url = m.optString("url", "");
            String sha256 = m.optString("sha256", "").toLowerCase();
            String sig = m.optString("sig", "");
            String minShell = m.optString("min_shell", "0.0.0");

            if (version.isEmpty() || url.isEmpty() || sha256.isEmpty()) {
                return new Result("error", null, "清单缺字段");
            }
            if (isBlacklisted(sp, version)) {
                return new Result("none", version, "该版本此前启动失败，已拉黑");
            }
            if (version.equals(activeVersion(sp)) || version.equals(sp.getString(K_PENDING, null))) {
                return new Result("none", version, "已是最新");
            }
            // 壳太老：新前端要的原生能力这个 APK 没有，硬换只会白屏。
            // 这时候该做的是提示换 APK，不是硬更新 —— 和桌面端 min_version 同一个意思。
            if (compareVersion(shellVersion, minShell) < 0) {
                return new Result("shell-too-old", version,
                        "需要 APP " + minShell + " 及以上，当前 " + shellVersion);
            }

            PublicKey pub = loadPublicKey(ctx);
            if (pub == null) {
                // 失败即拒绝：明文通道上，验不了签就等于谁都能推包进来
                return new Result("error", version, "包内没有验签公钥，拒绝更新");
            }
            if (!verifySignature(pub, sha256, sig)) {
                return new Result("error", version, "签名不对，拒绝更新");
            }

            byte[] zip = fetch(absolute(manifestUrl, url), MAX_BUNDLE_BYTES);
            String actual = sha256Hex(zip);
            if (!actual.equals(sha256)) {
                // 签名是对着清单里的 sha256 签的，所以这里再比一次实际内容，
                // 防的是「签名合法但下发的包被掉包」
                return new Result("error", version, "包内容与清单对不上");
            }

            File dir = bundleDir(ctx, version);
            deleteDir(dir);
            if (!dir.mkdirs()) return new Result("error", version, "建目录失败");
            unzip(zip, dir);
            if (!new File(dir, "index.html").isFile()) {
                deleteDir(dir);
                return new Result("error", version, "包里没有 index.html");
            }

            sp.edit().putString(K_PENDING, version)
                    .putLong(K_LAST_CHECK, System.currentTimeMillis()).apply();
            return new Result("ok", version, "下次启动生效");
        } catch (Exception e) {
            Log.w(TAG, "查更新失败", e);
            return new Result("error", null, String.valueOf(e.getMessage()));
        }
    }

    // ────────────────────────── 工具 ──────────────────────────

    /** 当前生效的前端包版本；null 表示用的是 APK 内置包 */
    public static String activeVersion(SharedPreferences sp) {
        String trial = sp.getString(K_TRIAL, null);
        return trial != null ? trial : sp.getString(K_LAST_GOOD, null);
    }

    public static String activeVersion(Context ctx) {
        return activeVersion(ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE));
    }

    public static String binaryVersionName(Context ctx) {
        try {
            PackageInfo pi = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            return pi.versionName == null ? "0.0.0" : pi.versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "0.0.0";
        }
    }

    private static String binaryVersionCode(Context ctx) {
        try {
            PackageInfo pi = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
            return String.valueOf(pi.versionCode);
        } catch (PackageManager.NameNotFoundException e) {
            return "0";
        }
    }

    private static File bundleRoot(Context ctx) {
        return new File(ctx.getFilesDir(), BUNDLE_DIR);
    }

    private static File bundleDir(Context ctx, String version) {
        // 版本号进路径前先洗一遍，别让 "../.." 这种东西决定我们往哪写
        return new File(bundleRoot(ctx), version.replaceAll("[^A-Za-z0-9._-]", "_"));
    }

    private static String goodDirPath(Context ctx, SharedPreferences sp) {
        String good = sp.getString(K_LAST_GOOD, null);
        if (good == null) return null;
        File d = bundleDir(ctx, good);
        return d.isDirectory() ? d.getAbsolutePath() : null;
    }

    /** 写 Capacitor 的偏好。null = 清掉，回到 APK 内置的 assets/public */
    private static void setServerBasePath(Context ctx, String path) {
        SharedPreferences.Editor e = ctx.getSharedPreferences(CAP_PREFS, Context.MODE_PRIVATE).edit();
        if (path == null) e.remove(CAP_SERVER_PATH);
        else e.putString(CAP_SERVER_PATH, path);
        e.apply();
    }

    private static void blacklist(SharedPreferences sp, String version) {
        Set<String> bad = new HashSet<>(Arrays.asList(
                sp.getString(K_BAD, "").split(",")));
        bad.remove("");
        bad.add(version);
        // 只留最近 20 个，别让这个字符串无限长
        List<String> keep = new ArrayList<>(bad);
        while (keep.size() > 20) keep.remove(0);
        // ⚠️ 不用 String.join：那是 API 26 才有的，minSdk 24 的机器上直接 NoSuchMethodError
        sp.edit().putString(K_BAD, android.text.TextUtils.join(",", keep)).apply();
    }

    private static boolean isBlacklisted(SharedPreferences sp, String version) {
        for (String v : sp.getString(K_BAD, "").split(",")) {
            if (v.equals(version)) return true;
        }
        return false;
    }

    private static void wipeBundles(Context ctx) {
        deleteDir(bundleRoot(ctx));
    }

    /** 只留当前这个版本，其余删掉 —— 否则每更新一次就多占一份磁盘 */
    private static void pruneOldBundles(Context ctx, String keep) {
        File[] dirs = bundleRoot(ctx).listFiles();
        if (dirs == null) return;
        File keepDir = bundleDir(ctx, keep);
        for (File d : dirs) {
            if (!d.equals(keepDir)) deleteDir(d);
        }
    }

    private static void deleteDir(File f) {
        if (f == null || !f.exists()) return;
        File[] kids = f.listFiles();
        if (kids != null) for (File k : kids) deleteDir(k);
        //noinspection ResultOfMethodCallIgnored
        f.delete();
    }

    private static byte[] fetch(String url, long maxBytes) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(10000);
        c.setReadTimeout(60000);
        c.setInstanceFollowRedirects(true);
        try {
            int code = c.getResponseCode();
            if (code != 200) throw new IOException("HTTP " + code + " @ " + url);
            try (InputStream in = c.getInputStream()) {
                ByteArrayOutputStream out = new ByteArrayOutputStream();
                byte[] buf = new byte[16 * 1024];
                int n;
                long total = 0;
                while ((n = in.read(buf)) > 0) {
                    total += n;
                    if (total > maxBytes) throw new IOException("响应超过上限 " + maxBytes);
                    out.write(buf, 0, n);
                }
                return out.toByteArray();
            }
        } finally {
            c.disconnect();
        }
    }

    /** 清单里的 url 允许写相对路径，按清单地址解析 */
    private static String absolute(String manifestUrl, String url) throws IOException {
        return new URL(new URL(manifestUrl), url).toString();
    }

    private static String sha256Hex(byte[] data) throws Exception {
        byte[] d = MessageDigest.getInstance("SHA-256").digest(data);
        StringBuilder sb = new StringBuilder(d.length * 2);
        for (byte b : d) sb.append(Character.forDigit((b >> 4) & 0xF, 16))
                           .append(Character.forDigit(b & 0xF, 16));
        return sb.toString();
    }

    private static PublicKey loadPublicKey(Context ctx) {
        try (InputStream in = ctx.getAssets().open(PUBLIC_KEY_ASSET)) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            String pem = new String(out.toByteArray(), StandardCharsets.UTF_8)
                    .replaceAll("-----[A-Z ]+-----", "")
                    .replaceAll("\\s", "");
            byte[] der = Base64.decode(pem, Base64.DEFAULT);
            return KeyFactory.getInstance("RSA").generatePublic(new X509EncodedKeySpec(der));
        } catch (Exception e) {
            Log.e(TAG, "读不到验签公钥，热更新已停用", e);
            return null;
        }
    }

    /** 签名对象是清单里那串 sha256 十六进制文本本身（发版脚本用 openssl 对同一串签） */
    private static boolean verifySignature(PublicKey pub, String sha256Hex, String sigB64) {
        try {
            Signature s = Signature.getInstance("SHA256withRSA");
            s.initVerify(pub);
            s.update(sha256Hex.getBytes(StandardCharsets.UTF_8));
            return s.verify(Base64.decode(sigB64, Base64.DEFAULT));
        } catch (Exception e) {
            Log.w(TAG, "验签出错", e);
            return false;
        }
    }

    /**
     * 解压。
     *
     * <p>⚠️ <b>必须挡 zip-slip</b>：条目名写成 {@code ../../databases/x} 就能写到
     * APP 私有目录之外去。判据是「解析后的绝对路径仍在目标目录之下」，
     * 不是「名字里没有 ..」—— 后者绕过方式太多。
     */
    private static void unzip(byte[] zip, File dest) throws IOException {
        String root = dest.getCanonicalPath() + File.separator;
        long total = 0;
        try (ZipInputStream zis = new ZipInputStream(new java.io.ByteArrayInputStream(zip))) {
            ZipEntry e;
            while ((e = zis.getNextEntry()) != null) {
                File target = new File(dest, e.getName());
                if (!target.getCanonicalPath().startsWith(root)) {
                    throw new IOException("包里有越界路径：" + e.getName());
                }
                if (e.isDirectory()) {
                    //noinspection ResultOfMethodCallIgnored
                    target.mkdirs();
                    continue;
                }
                File parent = target.getParentFile();
                if (parent != null) {
                    //noinspection ResultOfMethodCallIgnored
                    parent.mkdirs();
                }
                try (OutputStream os = new FileOutputStream(target)) {
                    byte[] buf = new byte[16 * 1024];
                    int n;
                    while ((n = zis.read(buf)) > 0) {
                        total += n;
                        if (total > MAX_UNPACKED_BYTES) throw new IOException("解压超过上限");
                        os.write(buf, 0, n);
                    }
                }
            }
        }
    }

    /** 语义版本比较：1.10.0 > 1.9.0（按段比数字，不能用字符串比） */
    static int compareVersion(String a, String b) {
        String[] x = a.split("[.-]"), y = b.split("[.-]");
        for (int i = 0; i < Math.max(x.length, y.length); i++) {
            int p = i < x.length ? parseIntSafe(x[i]) : 0;
            int q = i < y.length ? parseIntSafe(y[i]) : 0;
            if (p != q) return p < q ? -1 : 1;
        }
        return 0;
    }

    private static int parseIntSafe(String s) {
        try { return Integer.parseInt(s.trim()); } catch (Exception e) { return 0; }
    }
}
