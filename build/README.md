# 打包单机版 .exe

> 在你自己的 **Windows 机器**上跑一次，产出一个 `pms-demo.exe` 文件
> 客户拿到这个 .exe 就能双击运行，**完全不用装任何东西**

## 一次性环境准备（你的机器）

你的机器只需要装这两样（客户机器不用）：

| 软件 | 版本 | 下载 |
|---|---|---|
| Python | 3.11 或 3.12 | https://www.python.org/downloads/ （勾选 Add Python to PATH）|
| Node.js | 20 LTS | https://nodejs.org/zh-cn/ |

## 打包步骤

1. 进入 `v2/build/` 目录
2. **双击 `build_exe.bat`**
3. 等 8-15 分钟（首次会构建前端 + 装 PyInstaller）
4. 完成后看到 "打包完成！" 字样

## 产物在哪

```
v2/build/out/
├── pms-demo.exe         ← 大约 100-150 MB
└── 使用说明.txt
```

## 给客户

把整个 `out/` 文件夹压成 `pms-demo.zip` 发给客户。

客户操作：
1. 解压
2. 双击 `pms-demo.exe`
3. 等 10 秒，浏览器自动打开
4. 用 `admin / admin123` 登录

**完全不需要装 Python / Node / 任何东西。**

## 数据存哪

`.exe` 同目录的 `data\` 文件夹下（SQLite 单文件）。

- 备份：直接复制 `data\app.db`
- 重置：删掉 `data\` 文件夹，下次启动自动重建

## 重新打包（修改代码后）

直接再跑一次 `build_exe.bat`，会覆盖 `out/pms-demo.exe`。

## 常见问题

### Q：杀毒软件报毒

A：PyInstaller 打包出来的 .exe 经常被误报，因为它在运行时解压自己。是误报，把 .exe 加白名单即可。也可以买代码签名证书做签名。

### Q：第一次启动很慢

A：PyInstaller --onefile 模式启动前需要把 .exe 解压到临时目录。首次约 10 秒，之后几秒。

### Q：客户机器太老（Windows 7）

A：PyInstaller 默认只支持 Win 10+。Win 7 需要用 Python 3.8 + 旧版 PyInstaller 打包。

### Q：能不能不联网用

A：可以。.exe 自包含所有依赖，运行完全离线。
