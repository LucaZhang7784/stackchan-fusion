# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

# Add this line to disable the specific warning
add_compile_options(-Wno-missing-field-initializers)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
# "Trim" the build. Include the minimal set of components, main, and anything it depends on.
idf_build_set_property(MINIMAL_BUILD ON)

set(PROJECT_VER "0.0.2")
project(StackChan-XiaoZhi)

# ----------------------------------------------------------------------
# 硬件平台硬约束：本项目只支持 M5Stack Core S3（esp32s3）
#
# 烧录时报 "This chip is ESP32-S3, not ESP32" 多半是因为 sdkconfig 里
# CONFIG_IDF_TARGET 残留了 esp32（clone 后跑 build 没先 set-target 就会这样）。
# 在 cmake 阶段就拦截并给出明确修复命令，避免走到 esptool 才炸。
# ----------------------------------------------------------------------
if(NOT IDF_TARGET STREQUAL "esp32s3")
    message(FATAL_ERROR
        "\n"
        "============================================================\n"
        "  This project only supports M5Stack Core S3 (esp32s3).\n"
        "  Detected IDF_TARGET = '${IDF_TARGET}'.\n"
        "\n"
        "  Fix (run in this order, once after a fresh clone):\n"
        "    idf.py set-target esp32s3\n"
        "    idf.py fullclean\n"
        "    idf.py build flash\n"
        "============================================================\n"
    )
endif()

if(NOT CONFIG_BOARD_TYPE_M5STACK_CORE_S3)
    message(FATAL_ERROR
        "\n"
        "============================================================\n"
        "  CONFIG_BOARD_TYPE_M5STACK_CORE_S3 must be set to y.\n"
        "  This selects the M5Stack Core S3 board support code.\n"
        "\n"
        "  It is normally pulled in by sdkconfig.defaults. If you\n"
        "  see this error after a fresh clone, your sdkconfig may be\n"
        "  stale. Run:\n"
        "    rm -f sdkconfig sdkconfig.old\n"
        "    idf.py set-target esp32s3\n"
        "    idf.py reconfigure\n"
        "============================================================\n"
    )
endif()
