"""Распознавание текста на скриншотах встроенным в Windows 10/11 распознаванием
(Windows.Media.Ocr). Ничего устанавливать не нужно: используется PowerShell."""

import os
import subprocess
import tempfile

TIMEOUT = 90

# Скрипт PowerShell: открывает картинку, распознаёт текст (сначала английским
# движком — названия предметов MM2 английские, затем языком пользователя) и
# печатает найденные строки в UTF-8.
_SCRIPT = r"""
param([string]$Path)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($operation, [Type]$type) {
    $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
    $task.Wait(-1) | Out-Null
    $task.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = $null
try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US')) } catch {}
if ($engine -eq $null) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($engine -eq $null) { throw 'NO_OCR_ENGINE' }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) { [Console]::WriteLine($line.Text) }
"""


class OcrError(Exception):
    pass


def available():
    return os.name == "nt"


def recognize(image_bytes, suffix=".png"):
    """Строки текста со скриншота. OcrError — если распознать не удалось."""
    if not available():
        raise OcrError("распознавание скриншотов работает в Windows 10/11; вставьте список предметов текстом")
    try:
        workdir = tempfile.mkdtemp(prefix="mm2values-ocr-")
    except OSError as error:
        raise OcrError(f"не удалось создать временную папку: {error}") from error
    image = os.path.join(workdir, "shot" + (suffix if suffix.startswith(".") else ".png"))
    script = os.path.join(workdir, "ocr.ps1")
    try:
        try:
            with open(image, "wb") as file:
                file.write(image_bytes)
            with open(script, "w", encoding="utf-8-sig") as file:
                file.write(_SCRIPT)
        except OSError as error:
            raise OcrError(f"не удалось сохранить картинку во временную папку: {error}") from error
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-Path", image],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise OcrError(f"распознавание не запустилось: {error}") from error
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace")
            if "NO_OCR_ENGINE" in message:
                raise OcrError("в Windows не установлен язык распознавания текста (Параметры → Язык → английский)")
            raise OcrError("не удалось распознать картинку: " + (message.strip().splitlines() or ["ошибка"])[-1])
        text = result.stdout.decode("utf-8-sig", "replace")
        return [line.strip() for line in text.splitlines() if line.strip()]
    finally:
        for name in (image, script):
            try:
                os.unlink(name)
            except OSError:
                pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass
