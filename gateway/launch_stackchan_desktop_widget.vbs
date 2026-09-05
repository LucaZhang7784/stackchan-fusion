Set shell = CreateObject("WScript.Shell")
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
target = root & "\widget_bin\StackChanWidget.exe"
shell.Run Chr(34) & target & Chr(34), 0, False
