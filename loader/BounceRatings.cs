using System; using System.IO; using System.Linq; using System.Reflection; using System.Collections; using System.Collections.Generic; using System.Text; using System.Globalization;
using BepInEx; using HarmonyLib; using UnityEngine; using UnityEngine.SceneManagement;
// NBA Bounce Ratings: NBA Jam style 1-10 ratings per player, read from BepInEx/config/BounceRatings.csv at every scene load.
[BepInPlugin("richvam.bounce.ratings","Bounce Ratings","1.4.4")]
public class BounceRatings : BaseUnityPlugin {
  const BindingFlags BF=BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic;
  public static readonly string[] COLS={"speed","shooting","dunk","pass","steal","block","power"};
  static BepInEx.Logging.ManualLogSource L; static string PathCsv;
  static Dictionary<int,int[]> R=new Dictionary<int,int[]>();
  static Dictionary<int,float[]> Body=new Dictionary<int,float[]>(); // id -> {height_in, weight_lb}
  const float BASE_H=78f, BASE_BMI=25f, H_EXAG=1.0f;
  static Dictionary<object,int> ShotOwner=new Dictionary<object,int>();
  HashSet<int> done=new HashSet<int>(); float next; bool generated;
  static float Mul(int r,float perPoint){ return 1f+(Mathf.Clamp(r,1,10)-5)*perPoint; }
  void Awake(){ L=Logger; PathCsv=Path.Combine(Paths.ConfigPath,"BounceRatings.csv");
    Load(); SceneManager.sceneLoaded+=(s,m)=>{done.Clear(); AvState.Clear(); IsPortrait.Clear(); ShotOwner.Clear(); Load();};
    try{ var t=AccessTools.TypeByName("bounce.ExecuteShot");
      new Harmony("richvam.bounce.ratings").Patch(t.GetMethod("play",BF),prefix:new HarmonyMethod(typeof(BounceRatings).GetMethod("ShotPre",BindingFlags.Static|BindingFlags.NonPublic)));
    }catch(Exception e){L.LogError("Shot patch failed: "+e);}
    var h=new Harmony("richvam.bounce.ratings.extra");
    TryPatch(h,"bounce.PlayerDunk","activate","DunkPost"); TryPatch(h,"bounce.PlayerDunk","setMolested","DunkPost");
    TryPatch(h,"bounce.PlayerJumpBlockShot","activate","BlockStartPost"); TryPatch(h,"bounce.PlayerEntity","CanSucceedJumpShotBlock","BlockCheckPost"); }
  static void TryPatch(Harmony h,string type,string method,string post){ try{ var t=AccessTools.TypeByName(type); var m=AccessTools.Method(t,method);
      h.Patch(m,postfix:new HarmonyMethod(typeof(BounceRatings).GetMethod(post,BindingFlags.Static|BindingFlags.NonPublic))); L.LogInfo("Patched "+type+"."+method);
    }catch(Exception e){L.LogError("Patch "+type+"."+method+" failed: "+e.Message);} }
  static int[] RatingsOf(object player){ var av=F(player,"m_oAvatar"); if(av==null)return null; int id=Convert.ToInt32(F(av,"m_iLastSetUniqueId")); int[] v; return R.TryGetValue(id,out v)?v:null; }
  // Dunk: scales success chance (game uses 1.0 open, 0.7 contested). 1 = x0.6, 10 = x1.5 (capped at 100%)
  static void DunkPost(object __instance){ try{ var v=RatingsOf(F(__instance,"m_oPlayer")); if(v==null)return;
      float p=Convert.ToSingle(F(__instance,"m_fDunkSuccessProbability")); S(__instance,"m_fDunkSuccessProbability",Mathf.Clamp01(p*Mul(v[2],0.1f))); }catch{} }
  // Block: each block jump rolls once; ratings below 5 can cancel the block (1 = 60% kept). 5+ = game default.
  static Dictionary<object,bool> BlockOk=new Dictionary<object,bool>();
  static void BlockStartPost(object __instance){ try{ var pl=F(__instance,"m_oPlayer"); var v=RatingsOf(pl); if(pl==null)return;
      float keep=v==null?1f:Mathf.Clamp01(0.5f+0.1f*v[5]); BlockOk[pl]=UnityEngine.Random.value<keep; }catch{} }
  static void BlockCheckPost(object __instance,ref bool __result){ bool ok; if(__result&&BlockOk.TryGetValue(__instance,out ok)&&!ok) __result=false; }
  static object F(object o,string n){ if(o==null)return null; for(var t=o.GetType();t!=null;t=t.BaseType){var f=t.GetField(n,BF); if(f!=null)return f.GetValue(o);} return null; }
  static void S(object o,string n,object v){ for(var t=o.GetType();t!=null;t=t.BaseType){var f=t.GetField(n,BF); if(f!=null){f.SetValue(o,v);return;}} }
  static void Load(){ R.Clear(); if(!File.Exists(PathCsv))return;
    foreach(var line in File.ReadAllLines(PathCsv).Skip(1)){ var c=line.Split(','); if(c.Length<4+COLS.Length)continue;
      int id; if(!int.TryParse(c[0],out id))continue; var v=new int[COLS.Length];
      for(int i=0;i<COLS.Length;i++){int x; v[i]=int.TryParse(c[4+i].Trim(),out x)?Mathf.Clamp(x,1,10):5;} R[id]=v; }
    L.LogInfo("Loaded ratings for "+R.Count+" players");
    Body.Clear(); var bp=Path.Combine(Paths.ConfigPath,"BounceBodies.csv");
    if(File.Exists(bp)) foreach(var line in File.ReadAllLines(bp).Skip(1)){ var c=line.Split(','); if(c.Length<3)continue; int id; float h,w;
      if(int.TryParse(c[0],out id)&&float.TryParse(c[1],NumberStyles.Float,CultureInfo.InvariantCulture,out h)&&float.TryParse(c[2],NumberStyles.Float,CultureInfo.InvariantCulture,out w)) Body[id]=new[]{h,w}; }
    L.LogInfo("Loaded body sizes for "+Body.Count+" players"); }
  // Height/weight. 6'6" = 1.0; height differences exaggerated for arcade readability.
  static bool BodyScale(int id,out float hs,out float ws){ hs=ws=1f; float[] b; if(!Body.TryGetValue(id,out b))return false;
    hs=Mathf.Clamp(1f+(b[0]-BASE_H)/BASE_H*H_EXAG,0.75f,1.35f);
    float bmi=703f*b[1]/(b[0]*b[0]); ws=Mathf.Clamp(1f+(bmi-BASE_BMI)*0.015f,0.9f,1.15f); return true; }
  // Every SuperAvatar (match players, lineup/select screens) is scaled by the avatar it currently shows.
  static Dictionary<int,KeyValuePair<Vector3,int>> AvState=new Dictionary<int,KeyValuePair<Vector3,int>>();
  static Dictionary<int,bool> IsPortrait=new Dictionary<int,bool>();
  // Picker/menu portraits are framed head shots: scaling them pushes the head out of the tile, so leave them alone.
  static string Path0(Transform t){ var p=t.name; for(var x=t.parent;x!=null;x=x.parent) p=x.name+"/"+p; return p; }
  static bool CheckPortrait(Transform t){ string why=null;
    for(var p=t;p!=null&&why==null;p=p.parent){ var n=p.name.ToLowerInvariant(); if(n.Contains("portrait"))why="name:"+p.name;
      else foreach(var comp in p.GetComponents<Component>()){ if(comp==null)continue; var tn=comp.GetType().Name; if(tn.Contains("Portrait")||tn=="Canvas"){why="comp:"+tn+"@"+p.name;break;} } }
    L.LogDebug("[body] avatar "+(why==null?"SCALE ":"SKIP("+why+") ")+Path0(t)+" layer="+t.gameObject.layer);
    return why!=null; }
  static void ScaleAvatars(UnityEngine.Object[] mbs){
    foreach(var o in mbs){ if(o.GetType().Name!="SuperAvatar")continue; var c=(Component)o; if(!c.gameObject.scene.IsValid())continue;
      int key=c.GetInstanceID(); bool portrait; if(!IsPortrait.TryGetValue(key,out portrait)){ portrait=CheckPortrait(c.transform); IsPortrait[key]=portrait; }
      if(portrait)continue;
      int id=Convert.ToInt32(F(o,"m_iLastSetUniqueId")); var t=c.transform;
      KeyValuePair<Vector3,int> st; if(AvState.TryGetValue(key,out st)){ if(st.Value==id)continue; t.localScale=st.Key; }
      else st=new KeyValuePair<Vector3,int>(t.localScale,0);
      AvState[key]=new KeyValuePair<Vector3,int>(st.Key,id);
      float hs,ws; if(!BodyScale(id,out hs,out ws))continue;
      t.localScale=new Vector3(st.Key.x*ws*hs,st.Key.y*hs,st.Key.z*ws*hs); } }
  // Shooting: scales the game's own make chance (0-100, from release timing). 5 = unchanged, 10 = x1.5, 1 = x0.6
  static void ShotPre(object __instance){ int id; int[] v;
    if(!ShotOwner.TryGetValue(__instance,out id)||!R.TryGetValue(id,out v))return;
    if(Convert.ToBoolean(F(__instance,"m_bForceProbabiLity")))return;
    float p=Convert.ToSingle(F(__instance,"m_fProbability")); S(__instance,"m_fProbability",p*Mul(v[1],0.1f)); }
  void Generate(){
    var mbs=Resources.FindObjectsOfTypeAll(typeof(MonoBehaviour));
    var am=mbs.FirstOrDefault(o=>o.GetType().Name=="AvatarManager"); var list=F(am,"m_aoBundledAvatarContainers") as IEnumerable; if(list==null)return;
    var teamOf=new Dictionary<int,string>();
    var tm=mbs.FirstOrDefault(o=>o.GetType().Name=="BasketTeamManager");
    foreach(var ln in new[]{"m_aoBundledTeams","m_aoHardWoodBundledTeams","m_aoGoatBundledTeams"}){ var tl=F(tm,ln) as IEnumerable; if(tl==null)continue;
      foreach(var tc in tl){ var team=F(tc,"m_oTeam"); var mem=F(F(team,"m_oData"),"m_aoMembers") as IEnumerable; if(mem==null)continue;
        foreach(var m in mem){ int aid=Convert.ToInt32(F(m,"m_iAvatarUniqueId")); if(!teamOf.ContainsKey(aid)) teamOf[aid]=((F(team,"m_sName") as string)??"").Replace(","," "); } } }
    var sb=new StringBuilder("avatar_id,team,number,type,"+string.Join(",",COLS)+"\n"); int n=0;
    foreach(var c in list){ var d=F(c,"m_oAvatarData"); if(d==null)continue; int id=Convert.ToInt32(F(d,"m_iAvatarUniqueId")); string tn; teamOf.TryGetValue(id,out tn);
      sb.Append(id+","+(tn??"")+","+F(d,"m_iNumber")+","+F(d,"m_eAvatarType")+",5,5,5,5,5,5,5\n"); n++; }
    File.WriteAllText(PathCsv,sb.ToString(),new UTF8Encoding(false)); generated=true; L.LogInfo("Created "+PathCsv+" with "+n+" players"); Load(); }
  void Update(){
    if(Time.unscaledTime<next)return; next=Time.unscaledTime+0.2f;
    try{
      if(!generated&&!File.Exists(PathCsv)) Generate(); else generated=true;
      var allMb=Resources.FindObjectsOfTypeAll(typeof(MonoBehaviour)); ScaleAvatars(allMb);
      foreach(var pe in allMb.Where(o=>o.GetType().Name=="PlayerEntity").Cast<MonoBehaviour>()){
        if(!pe.gameObject.scene.IsValid()||done.Contains(pe.GetInstanceID()))continue;
        if(F(pe,"m_oMyTeam")==null)continue;
        var av=F(pe,"m_oAvatar"); if(av==null)continue; int id=Convert.ToInt32(F(av,"m_iLastSetUniqueId")); if(id==0)continue;
        done.Add(pe.GetInstanceID()); int[] v; if(!R.TryGetValue(id,out v)) v=new int[]{5,5,5,5,5,5,5};
        var shot=F(F(pe,"m_oPlayerShoot"),"m_oShot"); if(shot!=null) ShotOwner[shot]=id;
        var stats=F(pe,"m_oStats") as UnityEngine.Object; if(stats==null)continue;
        var cl=UnityEngine.Object.Instantiate(stats); if(cl is Component cc) cc.gameObject.SetActive(false);
        foreach(var f in cl.GetType().GetFields(BF)){ if(f.FieldType!=typeof(float))continue; float x=(float)f.GetValue(cl), m=1f;
          if(f.Name.StartsWith("m_fMaxSpeed")||f.Name.StartsWith("m_fAcceleration")) m=Mul(v[0],0.15f);   // speed: 1=0.4x, 10=1.75x
          else if(f.Name.StartsWith("m_fPassSpeed")) m=Mul(v[3],0.06f);                                    // pass
          else if(f.Name=="m_fStealMaxDistance"||f.Name=="m_fStealMaxAngleToTarget") m=Mul(v[4],0.08f);  // steal reach
          else if(f.Name=="m_fPushMaxDistance"||f.Name=="m_fPushMaxAngleToTarget") m=Mul(v[6],0.08f);    // power/push reach
          float hs,ws; if(BodyScale(id,out hs,out ws)){
            if(f.Name=="m_fPlayerRadius"||f.Name=="m_fContactRadius"||f.Name=="m_fContactRadiusBall") m*=ws*hs;   // body hitbox
            else if(f.Name=="m_fContactHeightBall") m*=hs;                                                          // vertical reach
            else if(f.Name=="m_fStealMaxDistance"||f.Name=="m_fPushMaxDistance") m*=hs;                             // arm reach
          }
          if(m!=1f) f.SetValue(cl,x*m); }
        S(pe,"m_oStats",cl);
        L.LogInfo("[rated] avatar "+id+" #"+F(av,"m_oNumber")+" "+string.Join(" ",COLS.Select((k,i)=>k+"="+v[i]).ToArray()));
      }
    }catch(Exception e){L.LogError(e);}
  }
}
