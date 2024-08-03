import ctypes
import sys
from ctypes import c_char_p, CFUNCTYPE

CALLBACK_FUNC_TYPE = CFUNCTYPE(None, c_char_p)


def run_vf3(pattern_data, target_data, options=b'-f vfe -u -s'):
    solutions = ''

    # 定义回调函数
    @CALLBACK_FUNC_TYPE
    def result_callback(all_solutions_c_str):
        nonlocal solutions
        solutions = all_solutions_c_str.decode('utf-8')

    try:
        # 系统判断，以决定加载的共享库是.so还是.dll
        if sys.platform.startswith('win'):
            lib_path = 'vf3/bin/vf3.dll'
            kernel32 = ctypes.WinDLL('kernel32.dll')
            handle = kernel32.LoadLibraryW(lib_path)
            libvf3 = ctypes.CDLL(lib_path)
        else:
            lib_path = 'vf3/bin/vf3.so'
            libvf3 = ctypes.CDLL(lib_path)

        # 设置 run_vf3 函数的参数类型和返回类型
        libvf3.run_vf3.argtypes = [c_char_p, c_char_p, c_char_p, CALLBACK_FUNC_TYPE]

        # 调用函数
        libvf3.run_vf3(c_char_p(pattern_data.encode('utf-8')), c_char_p(target_data.encode('utf-8')),
                       c_char_p(options.encode('utf-8')), result_callback)

    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(solutions)


if __name__ == "__main__":
    # 从命令行获取参数
    pattern_data = sys.argv[1]
    target_data = sys.argv[2]
    options = sys.argv[3] if len(sys.argv) > 3 else b'-f vfe -u -s'
    run_vf3(pattern_data, target_data, options)
