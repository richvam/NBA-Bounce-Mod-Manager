#!/bin/sh
# Builds a Bounce plugin: sh build.sh BounceStats   (output: loader/<name>.dll; prints only errors)
G="I:/SteamLibrary/steamapps/common/NBA BOUNCE"; M="$G/NBA Bounce_Data/Managed"; cd "$(dirname "$0")"
out=$(/c/Windows/Microsoft.NET/Framework64/v4.0.30319/csc.exe -nologo -nowarn:1701,1702 -t:library -out:"$1.dll" \
  -r:"$G/BepInEx/core/BepInEx.dll" -r:"$G/BepInEx/core/0Harmony.dll" -r:"$M/UnityEngine.dll" -r:"$M/UnityEngine.CoreModule.dll" \
  -r:"$M/UnityEngine.IMGUIModule.dll" -r:"$M/UnityEngine.TextRenderingModule.dll" -r:"$M/UnityEngine.UI.dll" -r:"$M/UnityEngine.UIModule.dll" -r:"$M/Unity.TextMeshPro.dll" \
  -r:"$M/Rewired_Core.dll" -r:"$M/Newtonsoft.Json.dll" -r:"$M/netstandard.dll" "$1.cs" 2>&1); rc=$?
echo "$out" | grep -E "error"; exit $rc
