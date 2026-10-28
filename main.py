"""程式進入點：python main.py 或由 PyInstaller 打包成執行檔。"""
import sys

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from pdftool.selftest import run
        args = sys.argv[sys.argv.index("--selftest") + 1:]
        sys.exit(run(args[0] if args else None))

    from pdftool.gui import main
    main()
