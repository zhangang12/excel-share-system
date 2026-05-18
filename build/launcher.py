"""单机版入口 —— PyInstaller 打包后由此启动"""
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def get_runtime_dir() -> Path:
    """获取 .exe 实际所在目录（用于持久化数据），不是 PyInstaller 临时解压目录"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def get_bundle_dir() -> Path:
    """获取 PyInstaller 内置数据目录（含前端静态文件等）"""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).parent.parent  # 开发模式：build/ 的父目录


def main():
    runtime_dir = get_runtime_dir()
    bundle_dir = get_bundle_dir()

    # 数据文件（SQLite）放 .exe 同级目录
    os.chdir(runtime_dir)
    data_dir = runtime_dir / "data"
    data_dir.mkdir(exist_ok=True)

    # 环境变量
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{data_dir / 'app.db'}"
    static_dir = bundle_dir / "web"
    if static_dir.exists():
        os.environ["STATIC_DIR"] = str(static_dir)

    # 启动后 3 秒打开浏览器
    def open_browser():
        time.sleep(3)
        try:
            webbrowser.open("http://127.0.0.1:8000")
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()

    # 打印欢迎信息
    print("=" * 60)
    print("  项目管理系统 · 单机版")
    print("=" * 60)
    print(f"  数据目录: {data_dir}")
    print(f"  访问地址: http://127.0.0.1:8000")
    print(f"  默认账号: admin / admin123")
    print(f"  关闭此窗口 = 停止服务")
    print("=" * 60)
    print()

    # 启动 uvicorn
    import uvicorn
    try:
        uvicorn.run(
            "app.main:app",
            host="127.0.0.1",
            port=8000,
            log_level="warning",  # 减少噪音
            access_log=False,
        )
    except KeyboardInterrupt:
        print("\n服务已停止。")
    except Exception as e:
        print(f"\n[错误] {e}")
        input("按回车键退出...")


if __name__ == "__main__":
    main()
