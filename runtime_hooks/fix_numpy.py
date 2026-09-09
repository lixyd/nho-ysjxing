"""
PyInstaller runtime hook: 修复 Anaconda numpy 在冻结环境中的导入错误。
numpy 2.x 会检测 __file__ 所在目录结构来判断是否在源码目录中,
在 PyInstaller 打包后可能误判。此 hook 通过预设环境变量来绕过检查。
"""
import os
import sys

if getattr(sys, 'frozen', False):
    # 设置标志, 让 numpy 跳过源码目录检查
    os.environ['_NUMPY_PYINSTALLER_FROZEN'] = '1'

    # 确保 _MEIPASS 在 sys.path 最前面
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass and meipass not in sys.path:
        sys.path.insert(0, meipass)

    # 清理可能导致 numpy 误判的路径
    cwd = os.getcwd()
    numpy_init = os.path.join(cwd, 'numpy', '__init__.py')
    if os.path.exists(numpy_init):
        # 工作目录包含 numpy 源码, 从 sys.path 中移除
        if cwd in sys.path:
            sys.path.remove(cwd)
        if '' in sys.path:
            sys.path.remove('')
