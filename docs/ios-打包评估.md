# iOS 打包评估（2026-08-06）

Android 版已经上线（Capacitor 7 + 本地包 + 热更新，见 `mobile/`）。
这份是「iOS 要不要做、怎么做、卡在哪」的调研结论。**下面每条都实测过或查了实物**，
没有「一般来说」。

---

## 一句话结论

**代码侧不是瓶颈，卡的是三件要花钱/要人办的事**：
Apple 开发者账号（$99/年）、一台装了 Xcode 的 Mac、**一个域名 + HTTPS**。
这三样齐了，iOS 版大约是 **2~3 天**的活（复用 Android 那套架构，多数是照着翻一遍 Swift）。

**最现实的分发路径：$99 个人/公司开发者账号 + TestFlight 内部测试**（100 人以内，不用审核）。

---

## 二、现在卡在哪（实测）

| 前提 | 现状 | 影响 |
|---|---|---|
| Apple 开发者账号 | **没有** | 没有它，装不到任何一台同事的手机上 |
| Xcode | **没装**（本机只有 Command Line Tools） | `xcodebuild` 用不了，CocoaPods 也没装 |
| 域名 + HTTPS | **没有**（`pms.tonghui-tech.com` 解析 NXDOMAIN，`nginx/certs` 是空的） | 见下面「ATS」和「装包」两条，这条影响最大 |

```
$ xcode-select -p        → /Library/Developer/CommandLineTools   （不是 Xcode）
$ host pms.tonghui-tech.com → NXDOMAIN
$ curl https://pms.tonghui-tech.com/ → 000（不通）
$ curl http://8.141.123.141/ → 200
```

---

## 三、⚠️ 最要紧的一条：服务器只有 IP、没有域名

iOS 的 **ATS（App Transport Security）默认禁止明文 HTTP**。绕过有两种写法：

1. `NSExceptionDomains` 按域名开个口子 —— **但 Apple 不允许把 IP 地址当作
   exception domain 的键**。我们的服务器是 `http://8.141.123.141`，**这条路走不通**。
2. `NSAllowsArbitraryLoads = true` 全局放开 —— 能用，但这是「把整扇门拆了」，
   而且上架审核时 Apple 会要求解释（TestFlight 内部测试不审核，暂时不挡路）。

**同一个域名问题还卡住了另一条路**：Ad Hoc 包用 `itms-services://` 链接装到手机上时，
**清单文件和 IPA 都必须走 HTTPS**。只有 IP + HTTP → 这条分发方式直接不可用。

> 所以：**先搞一个域名指向服务器、跑一次 `ops/enable-https.sh`（Let's Encrypt 不给纯 IP 签证书）**，
> 是性价比最高的一步 —— 它同时解决 ATS、Ad Hoc 装包，网页端也顺带不再是明文。

---

## 四、分发方式怎么选

| 方式 | 年费 | 能装多少 | 要 HTTPS | 要审核 | 结论 |
|---|---|---|---|---|---|
| **TestFlight 内部测试** | $99 | 100 人 | 否 | 否 | ✅ **推荐**。同事用 Apple ID 加进团队即可 |
| Ad Hoc + itms-services | $99 | 100 台/年 | **是** | 否 | 域名就绪后可作备选；每台设备要登记 UDID |
| 企业内部分发（Enterprise） | $299 | 不限 | 是 | 否 | ❌ 要求 100+ 员工 + D-U-N-S 编码，小厂不符合 |
| 上架 App Store | $99 | 不限 | 是 | **是** | ❌ 内部 ERP 没必要，且 ATS 全局例外大概率被问 |

⚠️ TestFlight 的包 **90 天过期**，到期要重新上传一版。内部测试员上限 100 人。

---

## 五、代码要做什么（复用 Android 那套，多数是翻译）

已经确认可以照搬的：

- **架构不用改**：Capacitor iOS 同样是「前端打进包、从本地加载」。
  iOS 默认 scheme 是 `capacitor`、hostname `localhost`（查了
  `CAPInstanceDescriptor.swift` 的默认值），所以页面 origin 是 `capacitor://localhost`。
- **后端 CORS 已经放行了 `capacitor://localhost`** —— 做 Android 时顺手加的，
  这块不用再动（`backend/app/main.py` 的 `_app_origins`）。
- **热更新机制能原样移植**：iOS 侧同样有 `setServerBasePath(_ path: String)`
  （`CapacitorBridge.swift:175`），换包=换目录，和 Android 一个路子。

要新写的（`mobile/android/.../*.java` 共 837 行，iOS 得有对应实现）：

| Android 现有 | 行数 | iOS 对应 | 难度 |
|---|---|---|---|
| `PmsUpdater` + `Plugin` | 531 | Swift 重写：下载/验签(RSA)/解压/试用回滚 | 中，逻辑照抄 |
| `PmsSpeechPlugin` | 163 | Swift + `SFSpeechRecognizer` | 中，API 不同但概念一致 |
| `MainActivity` | 143 | `AppDelegate`/`ViewController`：返回手势、下载、安全区 | 小 |
| 图标 | — | `make-icons.py` 扩几档 iOS 尺寸即可（源图是 1181px 透明 PNG） | 小 |

另外要配的：
- `Info.plist`：ATS 例外、`NSMicrophoneUsageDescription`、
  `NSSpeechRecognitionUsageDescription`（**少了这两条，一调用语音直接崩**）、
  方向锁定、显示名。
- CI：GitHub Actions 有 `macos-latest` 能跑 iOS 构建，**但签名证书要进 Secrets** ——
  ⚠️ 本仓库是**公开仓库**，且项目已明确「不把生产 SSH 私钥放 Actions Secrets」。
  签名证书比 SSH 私钥更敏感，建议**在本机 Xcode 出包**，别进 CI。

---

## 六、⚠️ 一条要提前知道的政策线：热更新

Apple 审核指南 **2.5.2** 限制 app 下载并执行代码。
我们的热更新只换 **WebView 里的 JS/CSS 资源包**（不是原生可执行代码），
这类做法（React Native CodePush、Capacitor Live Updates 都是）在实践中被接受，
前提是**不借此改变 app 的主要功能**。

TestFlight 内部测试不走审核，短期不受影响；但如果哪天要上架，这条要能说清楚。

**如果先不做 iOS 热更新**：每改一次前端都得重新出包、重新传 TestFlight、同事重新更新 ——
Android 那边现在是「后台静默拉取、下次打开生效」，两边体验差距会很明显。
所以 `PmsUpdater` 的 Swift 版建议一并做，不要留到二期。

---

## 七、建议的推进顺序

1. **业务侧先办两件事**（代码侧做不了）：
   - 注册 Apple 开发者账号（$99/年，公司主体需要邓白氏编码，个人主体当天就能下来）
   - **搞一个域名指向 8.141.123.141**，然后在服务器跑 `ops/enable-https.sh`
2. 本机装 Xcode + CocoaPods（约 10GB）
3. `npx cap add ios` → 配 Info.plist → 出图标 → 跑通真机调试
4. 翻 `PmsUpdater` / `PmsSpeech` 的 Swift 版
5. 上 TestFlight，拉同事进内部测试

第 1 步没办之前，第 2 步往后都是空转 —— **没有账号，连真机都装不上**。
