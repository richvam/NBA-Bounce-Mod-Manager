using System; using System.IO; using System.Linq; using System.Reflection; using System.Collections; using System.Collections.Generic; using System.Globalization; using System.Text;
using BepInEx; using HarmonyLib; using UnityEngine; using UnityEngine.SceneManagement;
// Bounce Roster: custom players from BepInEx/config/BounceRoster.csv (+ BounceRosterLooks.csv), GOAT chars in Quick Play.
// BounceRoster.csv:      id,name,team_id,number,role,template_id      (id <= -1000; team_id = BasketTeam unique id, e.g. Boston -1)
// BounceRosterLooks.csv: id,control,value                              (string / float / color #RRGGBB[AA])
[BepInPlugin("richvam.bounce.roster","Bounce Roster","1.2.0")]
public class BounceRoster : BaseUnityPlugin {
  const BindingFlags BF=BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic;
  const int GOAT=0x100;
  public class Entry{ public int id,team,number,template; public string name="",role=""; public List<string[]> looks=new List<string[]>(); }
  static BepInEx.Logging.ManualLogSource L; static Dictionary<int,Entry> Roster=new Dictionary<int,Entry>(); static string Dir;
  void Awake(){ L=Logger; Dir=Paths.ConfigPath; Load(); var h=new Harmony("richvam.bounce.roster");
    P(h,"bounce.SelectAvatarUI","getAllowedFilters",null,"FiltersPost");
    P(h,"bounce.AvatarManager","getAvatars","GetAvatarsPre","GetAvatarsPost");
    P(h,"bounce.AvatarManager","cleanAvatarsDLC",null,"InjectPost");
    P(h,"bounce.AvatarData","getNickname",null,"NickPost");
    SceneManager.sceneLoaded+=(sc,m)=>{ defaultsDone=false; }; }
  // Team defaults: replace a team's bundled default picks with its roster starters (only slots still holding
  // bundled avatars, so the user's own picks are kept). The picker opens on the page of the current pick,
  // so this also makes it open on page 1 with the team's players.
  bool defaultsDone; float nextDef;
  void Update(){ if(defaultsDone||Time.unscaledTime<nextDef||Roster.Count==0)return; nextDef=Time.unscaledTime+1f;
    try{ var tm=Resources.FindObjectsOfTypeAll(typeof(MonoBehaviour)).FirstOrDefault(o=>o.GetType().Name=="BasketTeamManager"&&((Component)o).gameObject.scene.IsValid()); if(tm==null)return;
      var teams=new List<object>(); var dict=F(tm,"m_dTeams") as IDictionary; if(dict!=null) foreach(var v in dict.Values) teams.Add(v);
      foreach(var ln in new[]{"m_aoBundledTeams"}){ var l=F(tm,ln) as IEnumerable; if(l!=null) foreach(var c in l){ var t=F(c,"m_oTeam"); if(t!=null&&!teams.Contains(t)) teams.Add(t); } }
      if(teams.Count==0)return; int changed=0;
      foreach(var t in teams){ var data=F(t,"m_oData"); if(data==null)continue; int tid=Convert.ToInt32(F(data,"m_iTeamUniqueId")??F(t,"m_iTeamUniqueId"));
        var starters=Roster.Values.Where(e=>e.team==tid&&e.role.StartsWith("start",StringComparison.OrdinalIgnoreCase)).OrderByDescending(e=>e.id).Select(e=>e.id).ToList(); if(starters.Count==0)continue;
        var mem=F(data,"m_aoMembers") as IList; if(mem==null)continue; int k=0;
        for(int i=0;i<mem.Count&&i<3&&k<starters.Count;i++){ var m=mem[i]; int cur=Convert.ToInt32(F(m,"m_iAvatarUniqueId"));
          if(cur>-1000&&cur!=tid){ S(m,"m_iAvatarUniqueId",starters[k]); changed++; } k++; } }
      defaultsDone=true; L.LogInfo("Team defaults: set "+changed+" slots to roster starters across "+teams.Count+" teams");
    }catch(Exception e){ defaultsDone=true; L.LogError("Team defaults: "+e); } }
  static List<string[]> Csv(string f){ var p=Path.Combine(Dir,f); var r=new List<string[]>(); if(!File.Exists(p))return r;
    foreach(var line in File.ReadAllLines(p,Encoding.UTF8).Skip(1)){ if(line.Trim().Length==0)continue; r.Add(SplitCsv(line)); } return r; }
  static string[] SplitCsv(string s){ var o=new List<string>(); var sb=new StringBuilder(); bool q=false;
    for(int i=0;i<s.Length;i++){ char c=s[i]; if(q){ if(c=='"'){ if(i+1<s.Length&&s[i+1]=='"'){sb.Append('"');i++;} else q=false; } else sb.Append(c); }
      else if(c=='"')q=true; else if(c==','){o.Add(sb.ToString());sb.Length=0;} else sb.Append(c); } o.Add(sb.ToString()); return o.ToArray(); }
  static int I(string s,int d){ int v; return int.TryParse(s.Trim(),NumberStyles.Integer,CultureInfo.InvariantCulture,out v)?v:d; }
  static void Load(){ Roster.Clear();
    foreach(var c in Csv("BounceRoster.csv")){ if(c.Length<6)continue; int id=I(c[0],0); if(id>-1000)continue;
      Roster[id]=new Entry{id=id,name=c[1].Trim(),team=I(c[2],0),number=I(c[3],0),role=c[4].Trim(),template=I(c[5],-33)}; }
    foreach(var c in Csv("BounceRosterLooks.csv")){ if(c.Length<3)continue; Entry e; if(Roster.TryGetValue(I(c[0],0),out e)) e.looks.Add(new[]{c[1].Trim(),c[2].Trim()}); }
    L.LogInfo("Roster: "+Roster.Count+" custom players"); }
  static void P(Harmony h,string t,string m,string pre,string post){ try{ var mi=AccessTools.Method(AccessTools.TypeByName(t),m);
      h.Patch(mi,pre==null?null:new HarmonyMethod(typeof(BounceRoster).GetMethod(pre,BindingFlags.Static|BindingFlags.NonPublic)),
                 post==null?null:new HarmonyMethod(typeof(BounceRoster).GetMethod(post,BindingFlags.Static|BindingFlags.NonPublic)));
      L.LogInfo("Patched "+t+"."+m); }catch(Exception e){L.LogError("Patch "+t+"."+m+" failed: "+e.Message);} }
  static object F(object o,string n){ if(o==null)return null; for(var t=o.GetType();t!=null;t=t.BaseType){var f=t.GetField(n,BF); if(f!=null)return f.GetValue(o);} return null; }
  static void S(object o,string n,object v){ for(var t=o.GetType();t!=null;t=t.BaseType){var f=t.GetField(n,BF); if(f!=null){f.SetValue(o,v);return;}} }
  static void FiltersPost(ref object __result){ try{
      if(__result is Array a){ var et=a.GetType().GetElementType(); foreach(var x in a) if(Convert.ToInt32(x)==GOAT) return;
        var n=Array.CreateInstance(et,a.Length+1); Array.Copy(a,n,a.Length); n.SetValue(Enum.ToObject(et,GOAT),a.Length); __result=n; }
      else if(__result is IList l){ var et=l.GetType().GetGenericArguments()[0]; foreach(var x in l) if(Convert.ToInt32(x)==GOAT) return; l.Add(Enum.ToObject(et,GOAT)); }
    }catch(Exception e){L.LogError("FiltersPost: "+e.Message);} }
  static void GetAvatarsPre(object _eFilter, ref bool _bIgnoreUnlocks){ if((Convert.ToInt32(_eFilter)&GOAT)!=0) _bIgnoreUnlocks=true; }
  static bool loggedTeam;
  // Picker order: the requested team's roster players first (starters, bench, legends), then everyone else.
  static void GetAvatarsPost(int _iTeamId, object __result){ try{
      if(!loggedTeam){ L.LogInfo("getAvatars teamId="+_iTeamId+" result="+(__result==null?"null":__result.GetType().Name)); loggedTeam=true; }
      var list=__result as IList; if(list==null||Roster.Count==0)return;
      var items=list.Cast<object>().ToList();
      Func<object,int> key=o=>{ Entry e; int id=Convert.ToInt32(F(o,"m_iAvatarUniqueId")); if(!Roster.TryGetValue(id,out e))return 1000;
        int r=e.role.StartsWith("start",StringComparison.OrdinalIgnoreCase)?0:e.role.StartsWith("bench",StringComparison.OrdinalIgnoreCase)?1:2;
        return e.team==_iTeamId?r:500+r; };
      var sorted=items.Select((o,i)=>new{o,i}).OrderBy(x=>key(x.o)).ThenBy(x=>x.i).Select(x=>x.o).ToList();
      if(list.IsFixedSize){ for(int i=0;i<sorted.Count;i++) list[i]=sorted[i]; } else { list.Clear(); foreach(var o in sorted) list.Add(o); }
    }catch(Exception e){L.LogError("GetAvatarsPost: "+e.Message);} }
  static readonly MethodInfo MWC=typeof(object).GetMethod("MemberwiseClone",BindingFlags.Instance|BindingFlags.NonPublic);
  static void SetVal(object v,string val){ int t=Convert.ToInt32(F(v,"m_controlValueType"));
    if(t==0) S(v,"m_string",val);
    else if(t==1){ float f; if(float.TryParse(val,NumberStyles.Float,CultureInfo.InvariantCulture,out f)) S(v,"m_float",Mathf.Clamp01(f)); }
    else if(t==2){ Color c; if(ColorUtility.TryParseHtmlString(val.StartsWith("#")?val:"#"+val,out c)) S(v,"m_color",new float[]{c.r,c.g,c.b,c.a}); } }
  static void ApplyLooks(object data,Entry e){ var list=F(data,"values") as IList; if(list==null)return;
    foreach(var lk in e.looks){ bool hit=false;
      foreach(var v in list){ var nm=F(v,"m_name") as string; if(nm==null)continue;
        if(nm!=lk[0]&&!nm.StartsWith(lk[0]+"_LINK_"))continue; SetVal(v,lk[1]); hit=true; }
      if(hit)continue;
      // Control missing on this template (e.g. armLeftColor exists on only a few avatars): add it by cloning a value of the same kind.
      int want=lk[0].EndsWith("Color")?2:(lk[1].StartsWith("#")?2:-1); object proto=null;
      foreach(var v in list){ int t=Convert.ToInt32(F(v,"m_controlValueType")); if(want==2&&t==2){proto=v;break;} }
      if(proto==null){ L.LogWarning("Player "+e.id+": template has no '"+lk[0]+"' control"); continue; }
      var nv=MWC.Invoke(proto,null); S(nv,"m_name",lk[0]); var col=F(proto,"m_color") as float[]; if(col!=null) S(nv,"m_color",(float[])col.Clone());
      SetVal(nv,lk[1]);
      if(list.IsFixedSize){ var arr=(Array)list; var na=Array.CreateInstance(arr.GetType().GetElementType(),arr.Length+1); Array.Copy(arr,na,arr.Length); na.SetValue(nv,arr.Length); S(data,"values",na); list=na; }
      else list.Add(nv); L.LogInfo("Player "+e.id+": added control "+lk[0]+"="+lk[1]); } }
  static void InjectPost(object __instance){ try{
      Load();
      var byId=F(__instance,"m_dAvatarsById") as IDictionary; var bundled=F(__instance,"m_dBundledAvatarsById") as IDictionary;
      var setAvatar=__instance.GetType().GetMethod("setAvatar",BF); int n=0;
      foreach(var e in Roster.Values){ try{
        object src=bundled!=null&&bundled.Contains(e.template)?bundled[e.template]:(byId!=null&&byId.Contains(e.template)?byId[e.template]:null);
        if(src==null){L.LogWarning("Template "+e.template+" missing for "+e.id);continue;}
        var data=src.GetType().GetMethod("Clone",BF).Invoke(src,null);   // always rebuilt from the file so edits apply
        S(data,"m_iAvatarUniqueId",e.id); S(data,"m_iNumber",e.number); ApplyLooks(data,e);
        var all=F(__instance,"m_aoAvatars") as IList;
        if(all!=null) for(int i=all.Count-1;i>=0;i--) if(Convert.ToInt32(F(all[i],"m_iAvatarUniqueId"))==e.id) all.RemoveAt(i);
        if(byId!=null&&byId.Contains(e.id)) byId.Remove(e.id);
        setAvatar.Invoke(__instance,new object[]{data,false}); n++; }catch(Exception ex){L.LogError("Player "+e.id+": "+ex.Message);} }
      L.LogInfo("Injected "+n+" roster players");
    }catch(Exception e){L.LogError("InjectPost: "+e);} }
  static void NickPost(object __instance, ref string __result){ try{ Entry e; if(Roster.TryGetValue(Convert.ToInt32(F(__instance,"m_iAvatarUniqueId")),out e)&&e.name.Length>0) __result=e.name; }catch{} }
}
