; ============================================================================
; 跳过 electron-builder 的「卸载旧版本」步骤
;
; 【为什么】
; 安装器默认会把旧版本的卸载程序复制到 %TEMP%\...\old-uninstaller.exe 静默执行
; （app-builder-lib/templates/nsis/include/installUtil.nsh:208 起）。部分用户机器上
; 这个进程会崩溃——崩溃点在 virtdisk.dll、异常码 0x7fffffff（不是合法的 Windows
; 异常码，是第三方错误收集器给的占位值），而 NSIS 卸载程序根本不会用虚拟磁盘 API，
; 所以基本可以判定是安全软件注入/拦截了这个「刚解压到 %TEMP% 的无签名 exe」。
;
; 【为什么必须治】
; 崩溃不是装完之后的噪音，是让升级彻底失败：
;   installUtil.nsh:213-241  退出码非 0 → 等 1 秒重试，最多 5 次（所以会弹好几个框）
;   installUtil.nsh:216-219  5 次都失败 → 弹「应用程序无法关闭」，默认 CANCEL
;   installUtil.nsh:128-132  $R0 仍非 0 → SetErrorLevel 2 + Quit，安装中止
; 结果就是那台机器永远停在旧版本。生产上 23 台客户端里有 8 台散落在
; 1.0.1 / 1.0.4 / 1.0.5 / 1.0.19 / 1.0.20，其中 3 台停在 1.0.1 十二天没动过。
;
; 【怎么治】
; uninstallOldVersion 一进门先读注册表的 UninstallString，读到空就直接 Return
; （installUtil.nsh:155-163），压根不会去复制和执行 old-uninstaller.exe。
; 而 customInit 跑在它之前（installer.nsi:79，在 .onInit 里；uninstallOldVersion
; 在 install section 里）。所以在这里把那个值删掉，整条崩溃路径就不存在了。
; 此时 $R0 = 0（installUtil.nsh:152-153 先 Push 0），错误标志也没置，
; handleUninstallResult 一路放行，安装正常继续。
;
; 【代价】
; 旧版本的文件不会被删除。本应用是 oneClick + perMachine:false，装在
; %LOCALAPPDATA%\Programs\ 下的同一个目录，新版本会整体覆盖，
; 只有「已从依赖里删掉的模块」会留下孤儿文件，慢慢占点磁盘。
; 相比「8 台机器永远升不了级」，这个代价可以接受。
;
; 【卸载入口不会丢】
; 安装收尾时会把 UninstallString / QuietUninstallString 重新写回
; （app-builder-lib/templates/nsis/include/installer.nsh:122-123），
; 控制面板里的卸载项照常可用。中间那几秒的空窗期只有安装中途失败才会碰到，
; 那种情况重装一次即可恢复。
; ============================================================================

!macro customInit
  ; SHELL_CONTEXT：本应用 perMachine=false，实际指向 HKCU；
  ; installSection.nsh:52/56 会分别用 SHELL_CONTEXT 和 HKEY_CURRENT_USER 各调一次，
  ; 两个都清掉才能保证不触发。
  DeleteRegValue SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY}" "UninstallString"
  DeleteRegValue HKEY_CURRENT_USER "${UNINSTALL_REGISTRY_KEY}" "UninstallString"

  ; 老 GUID 那套键（有历史包袱的项目才会定义），定义了就一并清
  !ifdef UNINSTALL_REGISTRY_KEY_2
    DeleteRegValue SHELL_CONTEXT "${UNINSTALL_REGISTRY_KEY_2}" "UninstallString"
    DeleteRegValue HKEY_CURRENT_USER "${UNINSTALL_REGISTRY_KEY_2}" "UninstallString"
  !endif

  ClearErrors
!macroend

; 兜底：万一还有别的原因让卸载步骤返回非 0，也不要中止安装。
; handleUninstallResult 里只要定义了这两个宏就直接 Return，跳过那个 Quit
; （installUtil.nsh:112-122）。上面删注册表已经能保证走不到那里，这里纯属双保险。
!macro customUnInstallCheck
!macroend

!macro customUnInstallCheckCurrentUser
!macroend
