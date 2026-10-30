using System; using System.Collections.Generic; using System.Reflection; using System.Reflection.Emit;
using BepInEx; using BepInEx.Configuration; using HarmonyLib; using UnityEngine.SceneManagement;
// NBA Bounce Push: allows shoving (the game's Push) in normal Bounce matches, not just GOAT mode.
// The game turns it off in three places: GameplayManager.initializeMode sets m_bDisablePush = isMainModeBounce(),
// PlayerEntity.toPush also requires isMainModeGoat(), and the AI's Statement_CanSuccedPush refuses in Bounce mode.
// Each check is wrapped so it reports "not Bounce" / "is GOAT" while EnableInBounceMode is on. Practice still
// sets m_bDisablePush itself, so the tutorials are unchanged.
[BepInPlugin("richvam.bounce.push","Bounce Push","1.0.0")]
public class BouncePush : BaseUnityPlugin {
  static BepInEx.Logging.ManualLogSource L; static ConfigEntry<bool> Enabled;
  void Awake(){ L=Logger;
    Enabled=Config.Bind("General","EnableInBounceMode",false,"Allow pushing in normal NBA Bounce matches (players and CPU).");
    SceneManager.sceneLoaded+=(s,m)=>{ try{ Config.Reload(); }catch{} };
    var h=new Harmony("richvam.bounce.push");
    TryPatch(h,"bounce.GameplayManager","initializeMode","isMainModeBounce","NotBounce");
    TryPatch(h,"bounce.Statement_CanSuccedPush","onEvaluate","isMainModeBounce","NotBounce");
    TryPatch(h,"bounce.PlayerEntity","toPush","isMainModeGoat","AsGoat"); }
  static string Want, Filter;
  static void TryPatch(Harmony h,string type,string method,string call,string filter){ try{
      var m=AccessTools.Method(AccessTools.TypeByName(type),method); Want=call; Filter=filter;
      h.Patch(m,transpiler:new HarmonyMethod(typeof(BouncePush).GetMethod("Wrap",BindingFlags.Static|BindingFlags.NonPublic)));
      L.LogInfo("Patched "+type+"."+method);
    }catch(Exception e){L.LogError("Patch "+type+"."+method+" failed: "+e.Message);} }
  // After every call to the mode check, pass its bool result through the filter.
  static IEnumerable<CodeInstruction> Wrap(IEnumerable<CodeInstruction> code){
    var f=typeof(BouncePush).GetMethod(Filter,BindingFlags.Static|BindingFlags.NonPublic); int n=0;
    foreach(var ci in code){ yield return ci;
      var mi=ci.operand as MethodInfo;
      if((ci.opcode==OpCodes.Call||ci.opcode==OpCodes.Callvirt)&&mi!=null&&mi.Name==Want){ n++; yield return new CodeInstruction(OpCodes.Call,f); } }
    if(n==0) L.LogWarning("No "+Want+" call found to wrap"); }
  static bool NotBounce(bool isBounce){ return isBounce&&!Enabled.Value; }
  static bool AsGoat(bool isGoat){ return isGoat||Enabled.Value; }
}
