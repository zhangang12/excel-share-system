package com.tonghui.pms;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 热更新的 JS 接口。真正的逻辑在 {@link PmsUpdater}，这里只做线程与参数转换。
 *
 * 前端用法见 frontend/src/h5/native.ts —— 那边**不 import @capacitor/core**，
 * 直接取 window.Capacitor.Plugins.PmsUpdater，所以 H5 包体积不受影响。
 */
@CapacitorPlugin(name = "PmsUpdater")
public class PmsUpdaterPlugin extends Plugin {

    /** 清单地址。跟 API 同一台服务器，随 APK 固化；换服务器要发新 APK。 */
    static final String MANIFEST_URL = "http://8.141.123.141/h5-ota/version.json";

    private final ExecutorService io = Executors.newSingleThreadExecutor();

    @PluginMethod
    public void notifyReady(PluginCall call) {
        PmsUpdater.markReady(getContext());
        MainActivity act = activity();
        if (act != null) act.onWebReady();
        call.resolve();
    }

    @PluginMethod
    public void notifyFailed(PluginCall call) {
        String reason = call.getString("reason", "前端上报启动失败");
        MainActivity act = activity();
        if (act != null) act.rollbackNow(reason);
        call.resolve();
    }

    @PluginMethod
    public void info(PluginCall call) {
        String active = PmsUpdater.activeVersion(getContext());
        JSObject r = new JSObject();
        r.put("shellVersion", PmsUpdater.binaryVersionName(getContext()));
        r.put("bundleVersion", active == null ? PmsUpdater.binaryVersionName(getContext()) : active);
        r.put("builtin", active == null);
        call.resolve(r);
    }

    @PluginMethod
    public void check(PluginCall call) {
        io.execute(() -> {
            PmsUpdater.Result res = PmsUpdater.check(
                    getContext(), MANIFEST_URL, PmsUpdater.binaryVersionName(getContext()));
            JSObject r = new JSObject();
            r.put("status", res.status);
            r.put("version", res.version);
            r.put("message", res.message);
            call.resolve(r);
        });
    }

    /** 启动后台自检一次。由 MainActivity 在页面起来之后调，不跟首屏抢带宽。 */
    void checkInBackground() {
        io.execute(() -> PmsUpdater.check(
                getContext(), MANIFEST_URL, PmsUpdater.binaryVersionName(getContext())));
    }

    private MainActivity activity() {
        return getActivity() instanceof MainActivity ? (MainActivity) getActivity() : null;
    }
}
