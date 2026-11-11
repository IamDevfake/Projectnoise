Set WshShell = CreateObject("WScript.Shell")
Dim fso, scriptDir, distDir, projectDir
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
distDir = fso.GetParentFolderName(scriptDir)
projectDir = fso.GetParentFolderName(distDir)
WshShell.CurrentDirectory = projectDir
WshShell.Run "pythonw """ & projectDir & "\denoise_gui.py""", 0, False
