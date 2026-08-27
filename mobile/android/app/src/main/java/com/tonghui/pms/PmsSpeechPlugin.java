package com.tonghui.pms;

import android.Manifest;
import android.content.Intent;
import android.os.Bundle;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;

import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.util.ArrayList;

/**
 * 语音输入 —— 把系统的 SpeechRecognizer 桥给 H5。
 *
 * <p><b>为什么非做不可</b>：Android WebView 里<b>根本没有 Web Speech API</b>。
 * {@code webkitSpeechRecognition} 是 Chrome 浏览器的能力，WebView 不绑定语音识别服务，
 * 一律取不到构造函数。所以旧壳里麦克风按钮永远是隐藏的 ——
 * 网页上能用、APP 里没有，这正是用户说的「内嵌页面不兼容」的一种。
 *
 * <p>识别由系统完成（多数机型走厂商或 Google 的服务），音频不经过我们的服务器、也不落库。
 * 前端约定见 frontend/src/h5/native.ts：partial / result / error / end 四个事件。
 */
@CapacitorPlugin(
        name = "PmsSpeech",
        permissions = { @Permission(alias = PmsSpeechPlugin.MIC, strings = { Manifest.permission.RECORD_AUDIO }) }
)
public class PmsSpeechPlugin extends Plugin {

    static final String MIC = "microphone";

    private SpeechRecognizer recognizer;

    @PluginMethod
    public void start(PluginCall call) {
        if (getPermissionState(MIC) != PermissionState.GRANTED) {
            // 权限没给就先要；用户点了允许再回到 startRecognition
            requestPermissionForAlias(MIC, call, "micResult");
            return;
        }
        startRecognition(call);
    }

    /**
     * 🆕 只要权限、不起识别 —— 给**云端录音**用。
     * 云端路径走 WebView 的 getUserMedia，而 WebView 只会放行**应用已经持有**的
     * 运行时权限，它自己不弹授权框。没有这个方法的话，用户得先去点一次原生识别
     * （失败）才能顺带把权限要到，太绕。
     */
    @PluginMethod
    public void ensureMic(PluginCall call) {
        if (getPermissionState(MIC) == PermissionState.GRANTED) {
            JSObject ret = new JSObject();
            ret.put("granted", true);
            call.resolve(ret);
            return;
        }
        requestPermissionForAlias(MIC, call, "micEnsureResult");
    }

    @PermissionCallback
    private void micEnsureResult(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("granted", getPermissionState(MIC) == PermissionState.GRANTED);
        call.resolve(ret);
    }

    @PermissionCallback
    private void micResult(PluginCall call) {
        if (getPermissionState(MIC) != PermissionState.GRANTED) {
            call.reject("需要允许麦克风权限");
            return;
        }
        startRecognition(call);
    }

    private void startRecognition(PluginCall call) {
        // ⚠️ SpeechRecognizer 只能在主线程创建和调用，放后台线程会静默什么都不发生
        getActivity().runOnUiThread(() -> {
            try {
                if (!SpeechRecognizer.isRecognitionAvailable(getContext())) {
                    // 有些国产 ROM 没装语音服务。老实说清楚，别让人对着按钮干等
                    call.reject("这台手机没有可用的语音识别服务");
                    return;
                }
                stopInternal();
                recognizer = SpeechRecognizer.createSpeechRecognizer(getContext());
                recognizer.setRecognitionListener(new Listener());

                String lang = call.getString("lang", "zh-CN");
                Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
                i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                        RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
                i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, lang);
                i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
                i.putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, getContext().getPackageName());

                recognizer.startListening(i);
                call.resolve();
            } catch (Exception e) {
                call.reject("语音识别启动失败：" + e.getMessage());
            }
        });
    }

    @PluginMethod
    public void stop(PluginCall call) {
        getActivity().runOnUiThread(this::stopInternal);
        call.resolve();
    }

    private void stopInternal() {
        if (recognizer != null) {
            try { recognizer.stopListening(); } catch (Exception ignored) { }
            try { recognizer.destroy(); } catch (Exception ignored) { }
            recognizer = null;
        }
    }

    @Override
    protected void handleOnDestroy() {
        getActivity().runOnUiThread(this::stopInternal);
    }

    private void emit(String event, String text) {
        JSObject d = new JSObject();
        if (text != null) d.put("text", text);
        notifyListeners(event, d);
    }

    private class Listener implements RecognitionListener {
        @Override public void onReadyForSpeech(Bundle params) { }
        @Override public void onBeginningOfSpeech() { }
        @Override public void onRmsChanged(float rmsdB) { }
        @Override public void onBufferReceived(byte[] buffer) { }
        @Override public void onEndOfSpeech() { }
        @Override public void onEvent(int eventType, Bundle params) { }

        @Override
        public void onPartialResults(Bundle partialResults) {
            String t = first(partialResults);
            if (t != null) emit("partial", t);
        }

        @Override
        public void onResults(Bundle results) {
            String t = first(results);
            if (t != null) emit("result", t);
            notifyListeners("end", new JSObject());
        }

        @Override
        public void onError(int error) {
            // 没说话 / 超时不该报红：用户就是点开了没开口，提示反而添乱
            if (error == SpeechRecognizer.ERROR_NO_MATCH
                    || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT) {
                notifyListeners("end", new JSObject());
                return;
            }
            String msg;
            switch (error) {
                case SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS: msg = "需要允许麦克风权限"; break;
                case SpeechRecognizer.ERROR_NETWORK:
                case SpeechRecognizer.ERROR_NETWORK_TIMEOUT: msg = "网络不通，语音识别用不了"; break;
                case SpeechRecognizer.ERROR_RECOGNIZER_BUSY: msg = "上一次识别还没结束"; break;
                default: msg = "语音识别不可用，请打字";
            }
            JSObject d = new JSObject();
            d.put("message", msg);
            notifyListeners("error", d);
        }

        private String first(Bundle b) {
            ArrayList<String> list = b == null ? null
                    : b.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
            return (list == null || list.isEmpty()) ? null : list.get(0);
        }
    }
}
