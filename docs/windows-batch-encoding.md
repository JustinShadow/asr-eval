# Windows 批处理乱码与“不是内部或外部命令”

> 症状：双击 `.bat` 后出现
> `'鐨勬槸鍑嵁閫氶亾...' 不是内部或外部命令`
> 或 `'evated.py' 不是内部或外部命令`，并伴随 `REM` 文本被当成命令。

## 原因

这是 **编码/代码页不匹配**，不是 Python 代码错误：

- `.bat` 文件保存为 UTF-8，但中文 Windows 的 `cmd.exe` 默认代码页是 GBK（936）；
- 中文字节被按 GBK 解释成乱码，`REM` 注释行和中文提示被当成命令执行；
- 如果文件使用 LF 换行，部分 `cmd.exe` 版本也会解析异常。

## 修复方式

### 方案 A：保存为 ANSI/GBK（最简单）

用记事本或 VS Code：

1. 另存为；
2. 编码选择 **ANSI**（中文 Windows 即 GBK）；
3. 换行选择 **CRLF**。

### 方案 B：保持 UTF-8，但在第一行切换代码页

```bat
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
python "%~dp0your_script.py"
if errorlevel 1 pause
```

注意：

- `chcp 65001` 必须在任何中文输出之前；
- `REM` 注释尽量用英文，避免老版本 `cmd.exe` 的解析问题；
- 路径含空格/中文时始终加双引号；
- 建议使用 CRLF。

### 方案 C：需要管理员权限时

```bat
@echo off
chcp 65001 >nul
powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath 'python' -ArgumentList '%~dp0elevated.py'"
```

不要在一个 `.bat` 里混用 `runas`、中文提示和未加引号的路径；失败时先看 `chcp` 与文件编码。

## 本项目

仓库内没有 `.bat`/`.cmd` 启动脚本，以上报错来自仓库外的本地批处理文件。若该文件由本项目的某个工具生成，请提供路径与内容，才能直接修脚本。