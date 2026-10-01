@echo off
rem Final 1440p footage for the AMS landing page. Resumable: frames already on disk are skipped,
rem so re-running this after an interruption continues where it stopped.
cd /d "%~dp0\.."
set BLENDER="F:\Blender\blender.exe"
set LOG=public\liquid\render_final.log
echo [%date% %time%] story start >> %LOG%
%BLENDER% -b --factory-startup --python blender\build_liquid.py -- --res 2560 --samples 256 --anim story >> %LOG% 2>&1
echo [%date% %time%] idle start >> %LOG%
%BLENDER% -b --factory-startup --python blender\build_liquid.py -- --res 2560 --samples 256 --anim idle >> %LOG% 2>&1
echo [%date% %time%] preparing web frames >> %LOG%
"C:\Users\vansh\AppData\Local\Programs\Python\Python311\python.exe" scripts\prepare_frames.py --quality 90 >> %LOG% 2>&1
echo [%date% %time%] done >> %LOG%
