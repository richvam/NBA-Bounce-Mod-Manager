using System; using System.IO; using System.Linq; using System.Reflection; using System.Collections; using System.Collections.Generic; using System.Globalization; using System.Text;
using BepInEx; using BepInEx.Configuration; using HarmonyLib; using UnityEngine; using UnityEngine.UI; using UnityEngine.EventSystems; using UnityEngine.SceneManagement; using Newtonsoft.Json; using TMPro;
// NBA Bounce Stats: per-player stat tracking for Season mode, for every player (bundled and BounceRoster customs).
// The game's own PlayerMatchStats only counts human-controlled players, so all six players are counted here from the
// game's events: ShootScoredEvent (PTS/FGM/3PM/dunks), ExecuteShot.play + BallEntity.dunked/dunkFail (FGA/3PA),
// GameUtils.pass + BallEntity.takeBall (assists, rebounds), BallBlockedEvent, PlayerSteal/PlayerIntercept (steals).
// Season games are saved per save slot in BepInEx/config/BounceStats/slot<N>.json, cleared when the slot is replaced or
// deleted. Games nobody plays (the human Simulate button, CPU vs CPU regular season, play-in, in-season
// tournament and playoffs) get stat lines made from the final score and each player's BounceRatings.csv ratings.
// The stats live in the game's own menus, built from clones of its standings page: four extra Season Info pages on the
// season screen (Leaders, Players, Game Log, Teams; Q/E to reach them, arrows to use them) and a Box Score button in the
// pause menu and on the end-of-match screen.
[BepInPlugin("richvam.bounce.stats","Bounce Stats","1.0.0")]
public class BounceStats : BaseUnityPlugin {
  const BindingFlags BF=BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic;
  const BindingFlags SF=BindingFlags.Static|BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.FlattenHierarchy;
  static BepInEx.Logging.ManualLogSource L; static string Dir; static System.Random Rng=new System.Random();
  static ConfigEntry<bool> AutoBox, SimStats;

  // ---------- data ----------
  public class Line{ public int id,team,pts,fgm,fga,tpm,tpa,reb,ast,stl,blk,dnk;
    public void Add(Line o){ pts+=o.pts;fgm+=o.fgm;fga+=o.fga;tpm+=o.tpm;tpa+=o.tpa;reb+=o.reb;ast+=o.ast;stl+=o.stl;blk+=o.blk;dnk+=o.dnk; } }
  public class Game{ public int seq,type,a,b,sa,sb; public bool sim,human; public List<Line> lines=new List<Line>(); }
  public class SeasonFile{ public int slot,userTeam; public string started=""; public List<Game> games=new List<Game>();
    public Dictionary<int,string> names=new Dictionary<int,string>(); public Dictionary<int,string> teams=new Dictionary<int,string>(); }
  public class Agg{ public int id,team,gp; public Line t=new Line(); }
  static Dictionary<int,SeasonFile> Slots=new Dictionary<int,SeasonFile>(); static bool dirty; static float saveAt;

  void Awake(){ L=Logger; Dir=Path.Combine(Paths.ConfigPath,"BounceStats"); Directory.CreateDirectory(Dir);
    AutoBox=Config.Bind("Stats","AutoOpenBoxScore",true,"Open the box score by itself after each season game");
    SimStats=Config.Bind("Stats","SimulatedStatLines",true,"Give simulated games stat lines made from the score and player ratings");
    for(int s=0;s<3;s++) Slot(s); LoadRatings(); var h=new Harmony("richvam.bounce.stats");
    var PE=T("bounce.PlayerEntity");
    // match tracking
    P(h,"bounce.MatchState","resetMatch",null,null,"ResetPost");
    C(h,"bounce.ShootScoredEvent",new[]{typeof(float),typeof(int),typeof(int),typeof(bool),typeof(bool),typeof(bool),PE,typeof(int),typeof(int),typeof(bool),typeof(int),typeof(int),typeof(bool)},"ScoredPost");
    P(h,"bounce.ExecuteShot","play",null,"ShotPre",null);
    P(h,"bounce.BallEntity","dunked",new[]{PE},"DunkPre",null);
    P(h,"bounce.BallEntity","dunkFail",new[]{PE},"DunkFailPre",null);
    P(h,"bounce.GameUtils","pass",new[]{PE,PE,typeof(float)},"PassPre",null);
    P(h,"bounce.BallEntity","takeBall",new[]{PE,typeof(bool)},null,"TakePost");
    P(h,"bounce.BallBlockedEvent","blockingPlayer",new[]{PE},"BlockPre",null);
    P(h,"bounce.PlayerSteal","update",null,"OwnerPre","StealPost");
    P(h,"bounce.PlayerIntercept","checkContact",null,"OwnerPre","StealPost");
    C(h,"bounce.MatchEndedEvent",new[]{T("bounce.MatchState")},"EndedPost");
    // season
    P(h,"bounce.SeasonManager","SetSelectedSeasonTournamentSlot",null,null,"SlotPost");
    P(h,"bounce.SeasonManager","CreateNewTournamentSlot",null,null,"NewSeasonPost");
    P(h,"bounce.SeasonManager","DeleteSeasonSlot",null,"DeletePre",null);
    P(h,"bounce.SeasonManager","MarkSeasonMatchAsCompleted",null,"CompletedPre",null);
    P(h,"bounce.SeasonMatch","SetRandomWinner",null,null,"RandomWinnerPost");
    P(h,"bounce.SeasonRegularPhase","SimulateOtherTeamsMatches",null,"OthersPre","OthersPost");
    P(h,"bounce.SeasonPlayOffsTournamentSaveData+SeasonPlayOffsKnockOut","SimulateNextRound",null,"SeriesPre","SeriesPost");
    // screens and input
    P(h,"bounce.ContinueSeasonMenuUI","OnEnable",null,null,"HubOn");
    P(h,"bounce.ContinueSeasonMenuUI","ManageScreens",null,null,"PagePost");
    P(h,"bounce.InGameUIPause","OnEnable",null,null,"PauseOn");
    P(h,"bounce.EndMatchUI","OnEnable",null,null,"EndOn");
    P(h,"bounce.RewiredManager","CheckPlayer",null,"CheckPlayerPre",null);
    foreach(var s in new[]{"MenuCanvas","EndMatchUI","InGameUIPause"}) P(h,"bounce."+s,"Update",null,"BlockUpdatePre",null);
    SceneManager.sceneLoaded+=(sc,m)=>{ if(boxOpen) CloseBox(); };
  }

  // ---------- reflection helpers ----------
  // Steal, shot and pass hooks run every frame while a play is happening, so every lookup by name is cached:
  // uncached AccessTools.TypeByName and GetField walks cost milliseconds and showed up as frame spikes.
  static Dictionary<string,Type> types=new Dictionary<string,Type>();
  static Dictionary<KeyValuePair<Type,string>,FieldInfo> fields=new Dictionary<KeyValuePair<Type,string>,FieldInfo>();
  static Dictionary<KeyValuePair<Type,string>,PropertyInfo> props=new Dictionary<KeyValuePair<Type,string>,PropertyInfo>();
  static Dictionary<KeyValuePair<Type,string>,MethodInfo> methods=new Dictionary<KeyValuePair<Type,string>,MethodInfo>();
  static Dictionary<string,PropertyInfo> insts=new Dictionary<string,PropertyInfo>();
  static Type T(string n){ Type t; if(!types.TryGetValue(n,out t)){ t=AccessTools.TypeByName(n); types[n]=t; } return t; }
  static FieldInfo FI(Type ty,string n){ var k=new KeyValuePair<Type,string>(ty,n); FieldInfo f;
    if(!fields.TryGetValue(k,out f)){ for(var t=ty;t!=null&&f==null;t=t.BaseType) f=t.GetField(n,BF); fields[k]=f; } return f; }
  static object F(object o,string n){ if(o==null)return null; var f=FI(o.GetType(),n); return f==null?null:f.GetValue(o); }
  static object Pr(object o,string n){ if(o==null)return null; var k=new KeyValuePair<Type,string>(o.GetType(),n); PropertyInfo p;
    if(!props.TryGetValue(k,out p)){ p=o.GetType().GetProperty(n,BF); props[k]=p; } return p==null?null:p.GetValue(o,null); }
  static object Call(object o,string n,params object[] a){ if(o==null)return null; var key=n; foreach(var x in a) key+="|"+x.GetType().Name;
    var k=new KeyValuePair<Type,string>(o.GetType(),key); MethodInfo m;
    if(!methods.TryGetValue(k,out m)){ m=AccessTools.Method(o.GetType(),n,a.Select(x=>x.GetType()).ToArray()); methods[k]=m; } return m==null?null:m.Invoke(o,a); }
  static int I(object o){ try{ return o==null?0:Convert.ToInt32(o); }catch{ return 0; } }
  static object Inst(string type){ PropertyInfo p; if(!insts.TryGetValue(type,out p)){ var t=T(type); if(t!=null){ p=t.GetProperty("Instance",SF);
        for(var b=t;p==null&&b!=null;b=b.BaseType) p=b.GetProperty("Instance",SF); } insts[type]=p; } return p==null?null:p.GetValue(null,null); }
  static void P(Harmony h,string t,string m,Type[] args,string pre,string post){ try{ var mi=AccessTools.Method(T(t),m,args);
      h.Patch(mi,pre==null?null:new HarmonyMethod(typeof(BounceStats).GetMethod(pre,SF)),post==null?null:new HarmonyMethod(typeof(BounceStats).GetMethod(post,SF)));
      L.LogInfo("Patched "+t+"."+m); }catch(Exception e){L.LogError("Patch "+t+"."+m+" failed: "+e.Message);} }
  static void C(Harmony h,string t,Type[] args,string post){ try{ var ci=AccessTools.Constructor(T(t),args);
      h.Patch(ci,null,new HarmonyMethod(typeof(BounceStats).GetMethod(post,SF))); L.LogInfo("Patched "+t+"..ctor"); }catch(Exception e){L.LogError("Patch "+t+"..ctor failed: "+e.Message);} }
  static int AvatarOf(object p){ var id=F(F(p,"m_oConfig"),"m_iAvatarUniqueId"); return id!=null?I(id):I(F(Call(F(p,"m_oAvatar"),"GetAvatarData"),"m_iAvatarUniqueId")); }
  static int TeamOf(object p){ return I(F(Pr(F(p,"m_oMyTeam"),"MyTeam"),"m_iTeamUniqueId")); }
  static FieldInfo ballField; static PropertyInfo gameTime;
  static object Ball(){ if(ballField==null) ballField=T("bounce.GameUtils").GetField("ms_oBall",SF); return ballField.GetValue(null); }
  static float GameTime(){ try{ if(gameTime==null) gameTime=T("bounce.GameplayManager").GetProperty("GameTime",SF); return Convert.ToSingle(gameTime.GetValue(null,null)); }catch{ return Time.time; } }
  static object Season(){ return Pr(Inst("bounce.SeasonManager"),"PlayingSeasonTournament"); }
  static string Name(int id){ string n; if(Names.TryGetValue(id,out n))return n;
    try{ n=Call(Call(Inst("bounce.AvatarManager"),"getAvatar",id),"getNickname") as string; }catch{} if(string.IsNullOrEmpty(n))n="#"+id; Names[id]=n; return n; }
  static string TeamName(int id){ string n; if(id==int.MinValue)return "CPU"; if(TeamNames.TryGetValue(id,out n))return n; var t=Call(Inst("bounce.BasketTeamManager"),"getTeam",id);
    n=F(t,"m_sAbbreviation") as string; if(string.IsNullOrEmpty(n))n=F(t,"m_sShortName") as string; if(string.IsNullOrEmpty(n))n="T"+id; TeamNames[id]=n; return n; }
  static Dictionary<int,string> Names=new Dictionary<int,string>(), TeamNames=new Dictionary<int,string>();

  // ---------- live match tracking ----------
  class Live{ public Line l=new Line(); public bool pending; }
  static Dictionary<object,Live> Cur=new Dictionary<object,Live>(); static bool practice, matchLive;
  static object passer, receiver; static float passT; static object endedMatch; static Game lastBox; static bool lastWasSeason;
  static Live Get(object p){ Live v; if(!Cur.TryGetValue(p,out v)){ v=new Live(); Cur[p]=v; } return v; }
  static bool Track(object p){ return p!=null&&!practice; }
  static void ResetPost(){ Cur.Clear(); passer=receiver=null; loose=false; endedMatch=null; matchLive=true;
    try{ practice=(bool)Call(Inst("bounce.GameCommon"),"isPractice"); }catch{ practice=false; } }
  static void ScoredPost(object __instance){ try{ var p=F(__instance,"_scoringPlayer"); if(!Track(p))return; var v=Get(p);
      bool three=(bool)(Pr(p,"LastShootIs3Point")??false), dunk=(bool)F(__instance,"m_bIsDunk");
      if(!v.pending){ v.l.fga++; if(three&&!dunk)v.l.tpa++; }   // tip-ins and deflections still count as attempts
      v.pending=false; v.l.fgm++; v.l.pts+=I(F(__instance,"_scorePoints")); if(three&&!dunk)v.l.tpm++; if(dunk)v.l.dnk++;
      if(passer!=null&&receiver==p&&passer!=p&&F(passer,"m_oMyTeam")==F(p,"m_oMyTeam")&&GameTime()-passT<=5f) Get(passer).l.ast++;
      passer=receiver=null; loose=false; }catch(Exception e){L.LogError("Scored: "+e.Message);} }
  static void ShotPre(object __instance){ try{ var p=F(__instance,"m_oPlayer"); if(!Track(p)||F(Ball(),"m_oPlayerOwner")!=p)return;
      var v=Get(p); v.l.fga++; if((bool)(Pr(p,"LastShootIs3Point")??false))v.l.tpa++; v.pending=true; loose=true; }catch(Exception e){L.LogError("Shot: "+e.Message);} }
  static void DunkPre(object __0){ if(!Track(__0))return; var v=Get(__0); v.l.fga++; v.pending=true; }
  static void DunkFailPre(object __0){ if(!Track(__0))return; Get(__0).l.fga++; loose=true; }
  static void PassPre(object __0,object __1){ passer=__0; receiver=__1; passT=GameTime(); }
  // ReboundEvent only fires for human players, so a rebound is whoever first gains the ball after a shot that didn't score.
  static bool loose;
  static void TakePost(object __0){ foreach(var v in Cur.Values) v.pending=false; if(loose&&Track(__0)){ Get(__0).l.reb++; loose=false; } if(receiver!=null&&__0!=receiver){ passer=receiver=null; } }
  static void BlockPre(object __instance,object __0){ if(!Track(__0))return; if((bool)F(__instance,"_isJumpBlock")||(bool)F(__instance,"m_bIsDunkBlock")) Get(__0).l.blk++; }
  static void OwnerPre(object __instance,out object[] __state){ __state=practice?null:new object[]{F(__instance,"m_bDone"),F(Ball(),"m_oPlayerOwner")}; }
  static void StealPost(object __instance,object[] __state){ try{ if(__state==null||__state[1]==null)return; var p=F(__instance,"m_oPlayer"); if(!Track(p))return; var prev=__state[1]; var now=F(Ball(),"m_oPlayerOwner");
      bool doneNow=__state[0]==null||(bool)(F(__instance,"m_bDone")??true), wasDone=__state[0]!=null&&(bool)__state[0];
      if(prev!=null&&now!=prev&&!wasDone&&doneNow&&F(prev,"m_oMyTeam")!=F(p,"m_oMyTeam")&&(now==null||now==p)) Get(p).l.stl++; }catch(Exception e){L.LogError("Steal: "+e.Message);} }
  // Box score of the match on court: every player, with or without stats.
  static Game LiveBox(){ var gm=Inst("bounce.GameplayManager"); var players=F(gm,"m_aoPlayers") as IList; if(players==null||players.Count==0)return null;
    var g=new Game(); var teams=F(gm,"m_aoTeams") as IList;
    if(teams!=null&&teams.Count>1){ g.a=I(F(Pr(teams[0],"MyTeam"),"m_iTeamUniqueId")); g.b=I(F(Pr(teams[1],"MyTeam"),"m_iTeamUniqueId")); g.sa=I(F(teams[0],"m_iScore")); g.sb=I(F(teams[1],"m_iScore")); }
    foreach(var p in players){ if(p==null)continue; var l=new Line(); Live v; if(Cur.TryGetValue(p,out v)) l.Add(v.l); l.id=AvatarOf(p); l.team=TeamOf(p); g.lines.Add(l); }
    return g; }
  static void EndedPost(object __0){ try{ endedMatch=__0; matchLive=false; if(practice)return; lastBox=LiveBox();
      if(lastBox!=null){ var sc=F(__0,"m_aiScore") as int[]; if(sc!=null&&sc.Length>1){ lastBox.sa=sc[0]; lastBox.sb=sc[1]; }
        lastBox.a=I(F(F(__0,"m_aoTeam0"),"m_iTeamUniqueId")); lastBox.b=I(F(F(__0,"m_aoTeam1"),"m_iTeamUniqueId")); }
      lastWasSeason=(bool)(Pr(Inst("bounce.SeasonManager"),"IsPlayingSeasonMatch")??false);
    }catch(Exception e){L.LogError("Ended: "+e.Message);} }

  // ---------- season storage ----------
  static int selSlot=-1;
  static string SlotPath(int s){ return Path.Combine(Dir,"slot"+s+".json"); }
  static SeasonFile Slot(int s){ SeasonFile f; if(Slots.TryGetValue(s,out f))return f;
    try{ if(File.Exists(SlotPath(s))) f=JsonConvert.DeserializeObject<SeasonFile>(File.ReadAllText(SlotPath(s))); }catch(Exception e){L.LogError("Read slot "+s+": "+e.Message);}
    if(f==null)f=new SeasonFile{slot=s}; Slots[s]=f; return f; }
  static void SaveAll(){ try{ foreach(var kv in Slots) if(kv.Value.games.Count>0||File.Exists(SlotPath(kv.Key))) File.WriteAllText(SlotPath(kv.Key),JsonConvert.SerializeObject(kv.Value)); dirty=false; }catch(Exception e){L.LogError("Save: "+e.Message);} }
  static void MarkDirty(){ dirty=true; saveAt=Time.unscaledTime+0.5f; ver++; }
  static int ver;
  // Empties a slot whose season was replaced or deleted.
  static void Reset(int s){ Slots[s]=new SeasonFile{slot=s}; try{ File.Delete(SlotPath(s)); }catch{} MarkDirty(); }
  static int CurSlot(){ var st=Season(); var list=Pr(Inst("bounce.SeasonManager"),"GetSeasonTournaments") as IList; return st==null||list==null?-1:list.IndexOf(st); }
  static SeasonFile Active(){ var st=Season(); int s=CurSlot(); if(st==null||s<0)return null; var f=Slot(s); int ut=I(F(st,"m_iPlayingTeamID"));
    if(f.games.Count>0&&f.userTeam!=ut) Reset(s); f=Slot(s); if(f.games.Count==0){ f.userTeam=ut; if(f.started=="")f.started=DateTime.Now.ToString("yyyy-MM-dd"); } return f; }
  static void Record(Game g){ var f=Active(); if(f==null)return; g.seq=f.games.Count; f.games.Add(g);
    foreach(var l in g.lines){ f.names[l.id]=Name(l.id); } f.teams[g.a]=TeamName(g.a); f.teams[g.b]=TeamName(g.b); MarkDirty(); }
  static void SlotPost(int index){ selSlot=index; }
  static void NewSeasonPost(int iSelectedTeamId){ int s=CurSlot(); if(s<0)s=selSlot; if(s<0)return; Reset(s); Slots[s].userTeam=iSelectedTeamId; Slots[s].started=DateTime.Now.ToString("yyyy-MM-dd"); MarkDirty(); }
  static void DeletePre(int iSeasonSlot){ Reset(iSeasonSlot); }
  static int GameType(){ try{ var st=Season(); var ms=F(st,"m_oSeasonMatches") as IList; int i=I(F(st,"m_iPlayingGameIndex")); return ms!=null&&i>=0&&i<ms.Count?I(F(ms[i],"m_eSeasonGamesType")):1; }catch{ return 1; } }
  static void CompletedPre(object oMatchState){ try{ var ms=oMatchState; var sc=F(ms,"m_aiScore") as int[];
      var g=new Game{type=GameType(),human=true,a=I(F(F(ms,"m_aoTeam0"),"m_iTeamUniqueId")),b=I(F(F(ms,"m_aoTeam1"),"m_iTeamUniqueId"))};
      if(sc!=null&&sc.Length>1){ g.sa=sc[0]; g.sb=sc[1]; }
      if(ms==endedMatch&&lastBox!=null){ g.lines=lastBox.lines; }                  // played on court
      else { g.sim=true; if(SimStats.Value) SimLines(g); }                          // the Simulate button
      Record(g); L.LogInfo("Season game recorded: "+TeamName(g.a)+" "+g.sa+"-"+g.sb+" "+TeamName(g.b)+(g.sim?" (simulated)":""));
    }catch(Exception e){L.LogError("Completed: "+e);} }
  static HashSet<object> seen=new HashSet<object>();
  static int UserTeam(){ return I(F(Season(),"m_iPlayingTeamID")); }
  static void Sim(int type,int a,int b,int sa,int sb){ if(Season()==null||a==UserTeam()||b==UserTeam())return; var g=new Game{type=type,a=a,b=b,sa=sa,sb=sb,sim=true}; if(SimStats.Value) SimLines(g); Record(g); }
  static void RandomWinnerPost(object __instance){ try{ if(!seen.Add(__instance))return;
      Sim(I(F(__instance,"m_eSeasonGamesType")),I(F(__instance,"m_iTeamAId")),I(F(__instance,"m_iTeamBId")),I(F(__instance,"m_iTeamAScore")),I(F(__instance,"m_iTeamBScore"))); }catch(Exception e){L.LogError("RandomWinner: "+e.Message);} }
  static IEnumerable<object> Records(){ var st=Season(); foreach(var n in new[]{"m_oWesternBasketTeamRecords","m_oEasternBasketTeamRecords"}){ var a=F(st,n) as IEnumerable; if(a!=null) foreach(var r in a) if(r!=null) yield return r; } }
  static void OthersPre(object oMatch,out Dictionary<int,int[]> __state){ __state=new Dictionary<int,int[]>(); try{
      var skip=new[]{UserTeam(),I(F(oMatch,"m_iTeamAId")),I(F(oMatch,"m_iTeamBId"))};   // the human's game is recorded on its own
      foreach(var r in Records()){ int id=I(F(r,"m_iPlayingTeamID")); if(!skip.Contains(id)) __state[id]=new[]{I(F(r,"m_iVictories")),I(F(r,"m_iLoses"))}; } }catch{} }
  // CPU regular-season games only move win/loss counts, so winners are paired with losers to make games.
  static void OthersPost(Dictionary<int,int[]> __state){ try{ var won=new List<int>(); var lost=new List<int>();
      foreach(var r in Records()){ int id=I(F(r,"m_iPlayingTeamID")); int[] o; if(!__state.TryGetValue(id,out o))continue;
        for(int i=o[0];i<I(F(r,"m_iVictories"));i++)won.Add(id); for(int i=o[1];i<I(F(r,"m_iLoses"));i++)lost.Add(id); }
      won=won.OrderBy(x=>Rng.Next()).ToList(); lost=lost.OrderBy(x=>Rng.Next()).ToList();
      for(int i=0;i<Math.Max(won.Count,lost.Count);i++){ int w=i<won.Count?won[i]:0, l=i<lost.Count?lost[i]:0; if(w==l)continue;
        int ws=Rng.Next(85,150), ls=Rng.Next(80,ws); if(w==0) w=NoTeam; if(l==0) l=NoTeam; Sim(1,w,l,ws,ls); }
    }catch(Exception e){L.LogError("Others: "+e.Message);} }
  // A CPU result with no matching opposite result gets an unnamed opponent, so no other team is credited a game.
  const int NoTeam=int.MinValue;
  static int[] Pair(object o,string n){ var p=F(o,n); return new[]{I(F(p,"First")),I(F(p,"Second"))}; }
  static void SeriesPre(object __instance,out int[] __state){ var a=Pair(__instance,"m_iTeamAScores"); var b=Pair(__instance,"m_iTeamBScores"); __state=new[]{a[0],a[1],b[0],b[1]}; }
  static void SeriesPost(object __instance,int[] __state){ try{ var a=Pair(__instance,"m_iTeamAScores"); var b=Pair(__instance,"m_iTeamBScores");
      if(a[0]+b[0]==__state[0]+__state[2])return; Sim(8,I(F(__instance,"m_iPlayerAID")),I(F(__instance,"m_iPlayerBID")),a[1]-__state[1],b[1]-__state[3]); }catch(Exception e){L.LogError("Series: "+e.Message);} }

  // ---------- simulated stat lines ----------
  static Dictionary<int,int[]> Ratings=new Dictionary<int,int[]>();   // id -> speed,shooting,dunk,pass,steal,block,power
  static void LoadRatings(){ Ratings.Clear(); var p=Path.Combine(Paths.ConfigPath,"BounceRatings.csv"); if(!File.Exists(p))return;
    foreach(var line in File.ReadAllLines(p).Skip(1)){ var c=line.Split(','); if(c.Length<11)continue; int id; if(!int.TryParse(c[0].Trim(),out id))continue;
      var v=new int[7]; for(int i=0;i<7;i++){ int x; v[i]=int.TryParse(c[4+i].Trim(),out x)?Mathf.Clamp(x,1,10):5; } Ratings[id]=v; } }
  static int[] Rat(int id){ int[] v; return Ratings.TryGetValue(id,out v)?v:new[]{5,5,5,5,5,5,5}; }
  static List<int> Roster(int team){ var r=new List<int>(); if(team==int.MinValue)return r; try{ var t=Call(Inst("bounce.BasketTeamManager"),"getTeam",team);
      var mem=F(F(t,"m_oData"),"m_aoMembers") as IEnumerable; if(mem!=null) foreach(var m in mem){ if(m==null)continue; bool mascot=false; try{ mascot=(bool)Call(m,"isMascot"); }catch{} if(!mascot) r.Add(I(F(m,"m_iAvatarUniqueId"))); } }catch{} return r; }
  static int Pick(List<int> ids,Func<int,double> w,int not){ double tot=0; foreach(var i in ids) if(i!=not) tot+=w(i); double x=Rng.NextDouble()*tot;
    foreach(var i in ids){ if(i==not)continue; x-=w(i); if(x<=0)return i; } return ids.FirstOrDefault(i=>i!=not); }
  static void SimLines(Game g){ var side=new[]{g.a,g.b}; var score=new[]{g.sa,g.sb}; var lines=new Dictionary<int,Line>[2]; var miss=new int[2]; var ros=new List<int>[2];
    for(int s=0;s<2;s++){ ros[s]=Roster(side[s]); lines[s]=new Dictionary<int,Line>(); foreach(var id in ros[s]) lines[s][id]=new Line{id=id,team=side[s]}; if(ros[s].Count==0)continue;
      var form=ros[s].ToDictionary(id=>id,id=>0.8+Rng.NextDouble()*0.4);   // hot and cold nights
      int left=score[s], guard=0;
      while(left>0&&guard++<600){ int id=Pick(ros[s],i=>(8+Rat(i)[1]+Rat(i)[2]*0.6+Rat(i)[0]*0.4)*form[i],int.MinValue); var r=Rat(id); var l=lines[s][id];
        if(left<2){ l.pts+=left; left=0; break; }            // bonus points
        bool three=left>=3&&Rng.NextDouble()<0.22+0.03*(r[1]-5); double pct=three?0.36+0.025*(r[1]-5):0.50+0.02*(r[1]-5)+0.01*(r[2]-5);
        l.fga++; if(three)l.tpa++;
        if(Rng.NextDouble()>pct){ miss[s]++; continue; }
        l.fgm++; if(three){ l.tpm++; l.pts+=3; left-=3; } else { l.pts+=2; left-=2; if(Rng.NextDouble()<0.18+0.04*(r[2]-5)) l.dnk++; }
        if(ros[s].Count>1&&Rng.NextDouble()<0.55) lines[s][Pick(ros[s],i=>3+Rat(i)[3],id)].ast++; } }
    for(int s=0;s<2;s++){ if(ros[s].Count==0)continue; double scale=Math.Max(0.3,score[s]/115.0);
      int reb=(int)Math.Round(miss[1-s]*0.72+miss[s]*0.28), stl=(int)Math.Round(Rng.Next(3,9)*scale), blk=(int)Math.Round(Rng.Next(1,6)*scale);
      for(int k=0;k<reb;k++) lines[s][Pick(ros[s],i=>6+Rat(i)[6]+Rat(i)[5]*0.5+Rat(i)[2]*0.3,int.MinValue)].reb++;
      for(int k=0;k<stl;k++) lines[s][Pick(ros[s],i=>3+Rat(i)[4],int.MinValue)].stl++;
      for(int k=0;k<blk;k++) lines[s][Pick(ros[s],i=>3+Rat(i)[5],int.MinValue)].blk++;
      g.lines.AddRange(lines[s].Values); } }

  // ---------- aggregation ----------
  static IEnumerable<Game> SeasonGames(){ var f=Active(); return f==null?Enumerable.Empty<Game>():f.games; }
  static Dictionary<int,Agg> Totals(IEnumerable<Game> games){ var d=new Dictionary<int,Agg>();
    foreach(var g in games) foreach(var l in g.lines){ Agg a; if(!d.TryGetValue(l.id,out a)){ a=new Agg{id=l.id}; d[l.id]=a; } a.gp++; a.team=l.team; a.t.Add(l); } return d; }
  // Pages re-render on every key press, so season totals are rebuilt only after a game is recorded.
  static Dictionary<int,Agg> cache; static int cacheVer=-1, cacheSlot=-2;
  static Dictionary<int,Agg> SeasonTotals(){ int s=CurSlot(); if(cache==null||cacheVer!=ver||cacheSlot!=s){ cache=Totals(SeasonGames()); cacheVer=ver; cacheSlot=s; } return cache; }
  static string NameAny(int id){ foreach(var f in Slots.Values){ string n; if(f.names.TryGetValue(id,out n))return n; } return Name(id); }
  static string Pct(int m,int a){ return a==0?"-":(100.0*m/a).ToString("0.0",CultureInfo.InvariantCulture); }
  static string Avg(int v,int gp){ return gp==0?"0.0":((double)v/gp).ToString("0.0",CultureInfo.InvariantCulture); }
  static object TeamObj(int id){ return id==NoTeam?null:Call(Inst("bounce.BasketTeamManager"),"getTeam",id); }
  static Sprite Icon(int id){ try{ return Call(TeamObj(id),"getIcon") as Sprite; }catch{ return null; } }
  static Color TeamColor(int id){ var c=F(TeamObj(id),"m_oButtonColor"); return c is Color?(Color)c:Navy; }
  static string TeamFull(int id){ var t=TeamObj(id); var n=Pr(t,"DisplayName") as string; if(string.IsNullOrEmpty(n))n=F(t,"m_sName") as string; return string.IsNullOrEmpty(n)?TeamName(id):n; }

  // ---------- native UI kit: every table is a clone of the season standings (SeasonBaseInfo) ----------
  static readonly Color Navy=new Color(0.047f,0.122f,0.4f), Crimson=new Color(0.592f,0.063f,0.165f), Mine=new Color(0.086f,0.149f,0.376f), Gold=new Color(1f,0.8f,0.1f);
  static GameObject holder;
  // Clones are made under an inactive holder so their scripts don't wake up before they are cleaned.
  static Transform Holder(){ if(holder==null){ holder=new GameObject("BounceStatsHolder"); holder.SetActive(false); DontDestroyOnLoad(holder); } return holder.transform; }
  static GameObject Template(){ var t=T("bounce.ContinueSeasonMenuUI"); foreach(var o in Resources.FindObjectsOfTypeAll(t)){ var c=o as Component;
      if(c!=null&&c.gameObject.scene.IsValid()){ var g=F(c,"m_oSeasonFirstMenu") as GameObject; if(g!=null)return g; } } return null; }
  static GameObject Clone(GameObject src){ var c=(GameObject)UnityEngine.Object.Instantiate(src,Holder()); c.name=src.name;
    foreach(var m in c.GetComponentsInChildren<MonoBehaviour>(true)){ var n=m.GetType().Name; if(n=="BasicLocalize"||n=="TeamRecord"||n=="UpdateNavigation") UnityEngine.Object.DestroyImmediate(m); }
    return c; }
  static Transform Kid(Transform t,string name){ if(t==null)return null; for(int i=0;i<t.childCount;i++) if(t.GetChild(i).name==name) return t.GetChild(i); return null; }
  static Transform Find(Transform t,string name){ if(t==null)return null; foreach(var x in t.GetComponentsInChildren<Transform>(true)) if(x.name==name) return x; return null; }
  static void Kill(Transform t){ if(t!=null) UnityEngine.Object.DestroyImmediate(t.gameObject); }
  static TMP_Text Tx(Transform t){ return t==null?null:t.GetComponent<TMP_Text>(); }
  // RTLTextMeshPro hides TMP's text property and redraws from its own originalText whenever the text changes shape,
  // so writing through a TMP_Text reference alone gets reverted; set both.
  static void Set(TMP_Text t,string s){ if(t==null)return; var f=FI(t.GetType(),"originalText"); if(f!=null) f.SetValue(t,s); if(t.text!=s) t.text=s; }
  static void W(Transform t,float w){ if(t==null)return; var rt=t as RectTransform; if(rt!=null) rt.sizeDelta=new Vector2(w,rt.sizeDelta.y);
    var le=t.GetComponent<LayoutElement>(); if(le==null) le=t.gameObject.AddComponent<LayoutElement>(); if(!le.ignoreLayout){ le.preferredWidth=w; le.minWidth=w; le.flexibleWidth=0; } }
  static void Place(Transform t,float x,float top,float w,float h){ var rt=(RectTransform)t; rt.anchorMin=rt.anchorMax=new Vector2(0.5f,0.5f); rt.pivot=new Vector2(0.5f,1f); rt.anchoredPosition=new Vector2(x,top); rt.sizeDelta=new Vector2(w,h); }
  static void Fit(TMP_Text t,float min,float max){ if(t==null)return; t.enableAutoSizing=true; t.fontSizeMin=min; t.fontSizeMax=max; }

  // A standings table: title, column headers and team rows (logo + coloured name plate + number cells).
  class Table{ public Transform root, head; public TMP_Text title; public List<Transform> rows=new List<Transform>(); float nameW; float[] colW;
    public static Table From(Transform conf){ var t=new Table{root=conf,title=Tx(Kid(conf,"ConferenceTitle")),head=Kid(conf,"TagsTitles")}; var kill=new List<Transform>();
      for(int i=0;i<conf.childCount;i++){ var c=conf.GetChild(i); if(c.name.StartsWith("TeamRecord")) t.rows.Add(c); else if(c!=t.head&&c.name!="ConferenceTitle") kill.Add(c); }
      foreach(var k in kill) Kill(k); var vl=conf.GetComponent<VerticalLayoutGroup>(); if(vl!=null) vl.childAlignment=TextAnchor.UpperCenter; Fit(t.title,26,50); return t; }
    public void Layout(float nw,float[] cw,int n){ nameW=nw; colW=cw; float tw=cw.Sum();
      while(rows.Count<n){ rows.Add(((GameObject)UnityEngine.Object.Instantiate(rows[rows.Count-1].gameObject,root)).transform); }
      while(rows.Count>n){ Kill(rows[rows.Count-1]); rows.RemoveAt(rows.Count-1); }
      Shape(head,true,tw); foreach(var r in rows) Shape(r,false,tw);
      ((RectTransform)root).sizeDelta=new Vector2(nw+tw,70+58+n*55.25f); if(title!=null) W(title.transform,nw+tw); }
    void Shape(Transform row,bool header,float tw){ W(row,nameW+tw); var plate=Kid(row,header?"TeamName":"Image"); W(plate,nameW);
      if(!header){ var hl=plate.GetComponent<HorizontalLayoutGroup>(); if(hl!=null) hl.enabled=false;   // logo and name are placed by hand
        var lo=Kid(plate,"TeamLogo") as RectTransform; if(lo!=null){ lo.anchorMin=lo.anchorMax=new Vector2(0,0.5f); lo.pivot=new Vector2(0.5f,0.5f); lo.anchoredPosition=new Vector2(62,0); }
        var nm=Kid(plate,"TeamName") as RectTransform; if(nm!=null){ nm.anchorMin=nm.anchorMax=new Vector2(0,0.5f); nm.pivot=new Vector2(0,0.5f); nm.anchoredPosition=new Vector2(136,0); nm.sizeDelta=new Vector2(nameW-142,52); }
        var t=Tx(nm); Fit(t,18,34); if(t!=null){ t.alignment=TextAlignmentOptions.Left; t.overflowMode=TextOverflowModes.Ellipsis; } }
      var tags=Kid(row,"Tags"); W(tags,tw); var cells=Cells(tags);
      while(cells.Count<colW.Length){ cells.Add(((GameObject)UnityEngine.Object.Instantiate(cells[cells.Count-1].gameObject,tags)).transform); }
      for(int i=colW.Length;i<cells.Count;i++) Kill(cells[i]);
      for(int i=0;i<colW.Length;i++){ W(cells[i],colW[i]); Fit(Tx(cells[i]),14,header?24:34); }
      if(!header) foreach(var n in new[]{"Image","SelfTeamMarket"}){ var bg=Kid(tags,n) as RectTransform; if(bg!=null) bg.sizeDelta=new Vector2(tw,bg.sizeDelta.y); } }
    static List<Transform> Cells(Transform tags){ var l=new List<Transform>(); if(tags!=null) for(int i=0;i<tags.childCount;i++){ var c=tags.GetChild(i); if(c.GetComponent<TMP_Text>()!=null) l.Add(c); } return l; }
    public void Head(string first,string[] h,int hi){ Set(Tx(Kid(head,"TeamName")),first); var cells=Cells(Kid(head,"Tags"));
      for(int i=0;i<cells.Count;i++){ var t=Tx(cells[i]); Set(t,i<h.Length?h[i]:""); t.color=i==hi?Gold:Color.white; } }
    public void Row(int i,int team,string name,string[] v,bool sel,bool mine){ if(i>=rows.Count)return; var r=rows[i]; r.gameObject.SetActive(true);
      var plate=Kid(r,"Image"); var img=plate.GetComponent<Image>(); if(img!=null) img.color=team==NoTeam?Navy:TeamColor(team);
      var logo=Kid(plate,"TeamLogo"); if(logo!=null){ var li=logo.GetComponent<Image>(); var sp=Icon(team); li.sprite=sp; li.enabled=sp!=null; }
      Set(Tx(Kid(plate,"TeamName")),name); var tags=Kid(r,"Tags"); var cells=Cells(tags); for(int k=0;k<cells.Count;k++) Set(Tx(cells[k]),k<v.Length?v[k]:"");
      var mk=Kid(tags,"SelfTeamMarket"); if(mk!=null){ mk.gameObject.SetActive(sel||mine); var mi=mk.GetComponent<Image>(); if(mi!=null) mi.color=sel?Crimson:Mine; } }
    public void Hide(int from){ for(int i=from;i<rows.Count;i++) rows[i].gameObject.SetActive(false); } }

  // ---------- season screen: extra Season Info pages ----------
  const int LEADERS=0, PLAYERS=1, LOG=2, TEAMS=3;
  class Page{ public GameObject go; public int kind; public TMP_Text title,sub,hint; public Image logo; public List<Table> tables=new List<Table>(); }
  static List<Page> pages=new List<Page>(); static object pagesHub;
  static Page MakePage(GameObject tpl,int kind,string title){ var go=Clone(tpl); go.name="BounceStats "+title; var t=go.transform; var p=new Page{go=go,kind=kind};
    Kill(Kid(t,"buttons")); Kill(Kid(t,"PopUpDeleteCurrentPlayoffs")); var side=Kid(t,"Image"); foreach(var n in new[]{"BG","TeamsGrid","Arrow","NextGamesText"}) Kill(Kid(side,n));
    p.title=Tx(Find(t,"submenuTitle")); Set(p.title,title); Fit(p.title,30,70); p.sub=Tx(Kid(side,"MatchTypeText")); Fit(p.sub,18,36);
    var lg=Find(Kid(t,"BG"),"PlayingTeamLogo"); p.logo=lg==null?null:lg.GetComponent<Image>();
    var west=Kid(t,"WesternConference"); var east=Kid(t,"EasternConference");
    var h=(GameObject)UnityEngine.Object.Instantiate(p.sub.gameObject,t); p.hint=h.GetComponent<TMP_Text>(); Place(h.transform,160,-410,1400,50); Fit(p.hint,18,28); p.hint.alignment=TextAlignmentOptions.Center;
    if(kind==LEADERS){ var confs=new List<Transform>{west,east}; for(int i=0;i<4;i++) confs.Add(((GameObject)UnityEngine.Object.Instantiate(east.gameObject,t)).transform);
      for(int i=0;i<6;i++){ Place(confs[i],-280+(i%3)*490,521-(i/3)*465,470,400); var tb=Table.From(confs[i]); tb.Layout(300,new float[]{70,100},5); p.tables.Add(tb); } }
    else { Kill(east); Place(west,140,521,1360,900); var tb=Table.From(west);
      if(kind==LOG) tb.Layout(420,new float[]{100,100,100,100,100,150,150,100},13); else tb.Layout(420,new float[]{80,120,120,120,120,120,130,130},kind==TEAMS?6:13); p.tables.Add(tb); }
    return p; }
  static void BuildPages(object hub){ foreach(var p in pages) if(p.go!=null) UnityEngine.Object.Destroy(p.go); pages.Clear(); pagesHub=hub;
    var tpl=F(hub,"m_oSeasonFirstMenu") as GameObject; var dict=F(hub,"m_oPossiblesScreensBySeasonState") as IDictionary; if(tpl==null||dict==null){ L.LogWarning("Season screen not found"); return; }
    pages.Add(MakePage(tpl,LEADERS,"Leaders")); pages.Add(MakePage(tpl,PLAYERS,"Players")); pages.Add(MakePage(tpl,LOG,"Game Log")); pages.Add(MakePage(tpl,TEAMS,"Teams"));
    var parent=tpl.transform.parent; var note=Kid(parent,"SeasonNotification"); int at=note!=null?note.GetSiblingIndex():parent.childCount;
    foreach(var p in pages){ p.go.SetActive(false); p.go.transform.SetParent(parent,false); p.go.transform.SetSiblingIndex(at++); }
    // Each season sub-state has its own list of pages; the game pages through them with Q/E (ManageScreens) and its animator.
    var done=new HashSet<object>(); foreach(var v in dict.Values){ var l=v as IList; if(l==null||!done.Add(l))continue; foreach(var p in pages) l.Add(p.go); }
    L.LogInfo("Added "+pages.Count+" stats pages to the season screen"); }
  static Page CurrentPage(object hub){ try{ var dict=F(hub,"m_oPossiblesScreensBySeasonState") as IDictionary; var key=F(Season(),"m_eSeasonTournamentSubState");
      if(dict==null||key==null||!dict.Contains(key))return null; var l=dict[key] as IList; int i=I(F(hub,"iSelectionIndex")); if(l==null||i<0||i>=l.Count)return null;
      var go=l[i] as GameObject; return pages.FirstOrDefault(p=>p.go==go); }catch{ return null; } }
  static bool Popup(object hub){ foreach(var n in new[]{"m_oConfirmPopUp","m_oSeasonSettingsPanel","m_oSeasonNotificationPopUp"}){ var c=F(hub,n) as Component; if(c!=null&&c.gameObject.activeSelf) return true; } return false; }
  static void HubOn(object __instance){ hub=__instance; try{ if(pagesHub!=__instance||pages.Count==0||pages[0].go==null) BuildPages(__instance); foreach(var p in pages) if(p.go.activeInHierarchy) Render(p); }catch(Exception e){L.LogError("Season pages: "+e);} }
  static void PagePost(object __instance){ var p=CurrentPage(__instance); if(p!=null) Render(p); }

  static int sortCol=1, selRow, logTop, teamIdx, logFor=int.MinValue; static bool teamInit;
  static readonly string[] PCols={"GP","PPG","RPG","APG","SPG","BPG","FG Pct","3P Pct"}, LCols={"PTS","REB","AST","STL","BLK","FG","3P","DNK"};
  static double Key(Agg a,int c){ switch(c){ case 0:return a.gp; case 1:return (double)a.t.pts/a.gp; case 2:return (double)a.t.reb/a.gp; case 3:return (double)a.t.ast/a.gp;
      case 4:return (double)a.t.stl/a.gp; case 5:return (double)a.t.blk/a.gp; case 6:return a.t.fga<a.gp?0:(double)a.t.fgm/a.t.fga; default:return a.t.tpa<a.gp?0:(double)a.t.tpm/a.t.tpa; } }
  static string[] PCells(Agg a){ return new[]{""+a.gp,Avg(a.t.pts,a.gp),Avg(a.t.reb,a.gp),Avg(a.t.ast,a.gp),Avg(a.t.stl,a.gp),Avg(a.t.blk,a.gp),Pct(a.t.fgm,a.t.fga),Pct(a.t.tpm,a.t.tpa)}; }
  static string[] LCells(Line l){ return new[]{""+l.pts,""+l.reb,""+l.ast,""+l.stl,""+l.blk,l.fgm+"-"+l.fga,l.tpm+"-"+l.tpa,""+l.dnk}; }
  static List<Agg> Sorted(){ return SeasonTotals().Values.Where(a=>a.gp>0).OrderByDescending(a=>Key(a,sortCol)).ThenByDescending(a=>Key(a,1)).ToList(); }
  static void Side(Page p,Sprite logo,string sub){ if(p.logo!=null){ p.logo.sprite=logo; p.logo.enabled=logo!=null; } Set(p.sub,sub); }
  // Text set on a clone before it has ever been active is replaced by its saved text when it wakes, so only active pages render.
  static void Render(Page p){ if(p.go==null||!p.go.activeInHierarchy)return; try{ var f=Active(); int ut=f==null?0:f.userTeam; var tot=SeasonTotals().Values.Where(a=>a.gp>0).ToList();
      if(p.kind==LEADERS){ Side(p,Icon(ut),"Per game"); Set(p.hint,tot.Count==0?"No games yet: play or simulate a season game":"Top 5 per game     Field goal pct: 3+ attempts a game");
        int maxGp=tot.Count==0?0:tot.Max(a=>a.gp); var pool=tot.Where(a=>a.gp*2>=maxGp||tot.Count<12).ToList();
        var names=new[]{"Points","Rebounds","Assists","Steals","Blocks","Field Goal Pct"}; var keys=new[]{1,2,3,4,5,6};
        for(int c=0;c<6;c++){ var tb=p.tables[c]; int k=keys[c]; Set(tb.title,names[c]); tb.Head("",new[]{"GP",c==5?"PCT":"AVG"},1);
          var top=(c==5?pool.Where(a=>a.t.fga>=3*a.gp):pool).OrderByDescending(a=>Key(a,k)).Take(5).ToList();
          for(int i=0;i<top.Count;i++){ var a=top[i]; tb.Row(i,a.team,(i+1)+". "+NameAny(a.id),new[]{""+a.gp,c==5?Pct(a.t.fgm,a.t.fga):Avg(c==0?a.t.pts:c==1?a.t.reb:c==2?a.t.ast:c==3?a.t.stl:a.t.blk,a.gp)},false,a.team==ut); }
          tb.Hide(top.Count); } }
      else if(p.kind==PLAYERS){ var tb=p.tables[0]; var list=Sorted(); selRow=Math.Max(0,Math.Min(selRow,list.Count-1)); int n=tb.rows.Count, first=Math.Max(0,Math.Min(selRow-n/2,list.Count-n));
        Set(tb.title,"All Players  -  sorted by "+PCols[sortCol]); tb.Head("",PCols,sortCol);
        for(int i=0;i<n&&first+i<list.Count;i++){ var a=list[first+i]; tb.Row(i,a.team,(first+i+1)+". "+NameAny(a.id),PCells(a),first+i==selRow,a.team==ut); } tb.Hide(Math.Max(0,list.Count-first));
        if(list.Count>0){ var s=list[selRow]; logFor=s.id; Side(p,Icon(s.team),NameAny(s.id)); } else Side(p,Icon(ut),"No games yet");
        Set(p.hint,"Up/Down: player     Left/Right: sort     E: that player's game log"); }
      else if(p.kind==LOG){ var tb=p.tables[0]; if(logFor==int.MinValue&&tot.Count>0) logFor=Sorted()[0].id; int id=logFor;
        var rows=SeasonGames().SelectMany(g=>g.lines.Where(l=>l.id==id).Select(l=>new{g,l})).Reverse().ToList(); int n=tb.rows.Count; logTop=Math.Max(0,Math.Min(logTop,rows.Count-n));
        Set(tb.title,id==int.MinValue?"Game Log":NameAny(id)+"  -  "+rows.Count+" games"); tb.Head("",LCols,-1);
        for(int i=0;i<n&&logTop+i<rows.Count;i++){ var x=rows[logTop+i]; var g=x.g; var l=x.l; int opp=l.team==g.a?g.b:g.a; bool won=l.team==g.a?g.sa>g.sb:g.sb>g.sa;
          tb.Row(i,opp,(won?"W ":"L ")+(l.team==g.a?g.sa+"-"+g.sb:g.sb+"-"+g.sa)+" vs "+TeamName(opp)+(g.sim?" *":""),LCells(l),false,false); } tb.Hide(Math.Max(0,rows.Count-logTop));
        var la=rows.Count>0?rows[0].l.team:ut; Side(p,Icon(la),id==int.MinValue?"No games yet":NameAny(id));
        Set(p.hint,"Up/Down: scroll     * = simulated game     Pick the player on the Players page"); }
      else { var tb=p.tables[0]; var games=SeasonGames().ToList(); var ids=games.SelectMany(g=>new[]{g.a,g.b}).Where(x=>x!=NoTeam).Distinct().OrderBy(x=>TeamName(x)).ToList();
        if(ids.Count==0){ Set(tb.title,"Teams"); tb.Head("",PCols,-1); tb.Hide(0); Side(p,Icon(ut),"No games yet"); Set(p.hint,""); return; }
        if(!teamInit){ teamInit=true; teamIdx=ids.Contains(ut)?ids.IndexOf(ut):0; } int t=ids[((teamIdx%ids.Count)+ids.Count)%ids.Count];
        var tg=games.Where(g=>g.a==t||g.b==t).ToList(); int w=tg.Count(g=>g.a==t?g.sa>g.sb:g.sb>g.sa);
        var pl=Totals(tg).Values.Where(a=>a.team==t).OrderByDescending(a=>Key(a,1)).ToList(); var team=new Agg{id=0,team=t,gp=tg.Count}; foreach(var a in pl) team.t.Add(a.t);
        Set(tb.title,"<   "+TeamFull(t)+"   >"); tb.Head("",PCols,-1); int n=Math.Min(pl.Count,tb.rows.Count-1);
        for(int i=0;i<n;i++) tb.Row(i,t,NameAny(pl[i].id),PCells(pl[i]),false,false); tb.Row(n,t,"Team",PCells(team),true,false); tb.Hide(n+1);
        Side(p,Icon(t),w+" - "+(tg.Count-w)); Set(p.hint,"Left/Right: team     Team row: points per game and team shooting"); }
    }catch(Exception e){ L.LogError("Render "+p.kind+": "+e); } }
  static void HubInput(){ if(hub==null)return; var p=CurrentPage(hub); if(p==null||Popup(hub))return; bool ch=true;
    if(Btn("UIDown",true)){ if(p.kind==PLAYERS)selRow++; else if(p.kind==LOG)logTop++; else ch=false; }
    else if(Btn("UIUp",true)){ if(p.kind==PLAYERS)selRow=Math.Max(0,selRow-1); else if(p.kind==LOG)logTop=Math.Max(0,logTop-1); else ch=false; }
    else if(Btn("UIRight",true)){ if(p.kind==PLAYERS)sortCol=Math.Min(PCols.Length-1,sortCol+1); else if(p.kind==TEAMS)teamIdx++; else ch=false; }
    else if(Btn("UILeft",true)){ if(p.kind==PLAYERS)sortCol=Math.Max(0,sortCol-1); else if(p.kind==TEAMS)teamIdx--; else ch=false; }
    else ch=false;
    if(ch){ if(p.kind==PLAYERS) logTop=0; Render(p); } }

  // ---------- box score panel (pause menu and end of match) ----------
  class Box{ public GameObject go; public TMP_Text title; public Table a,b; }
  static Box box; static bool boxOpen; static int boxOpened, boxClosed=-10; static GameObject boxReturn; static Behaviour module;
  static Box MakeBox(Transform parent){ var tpl=Template(); if(tpl==null){ L.LogWarning("Box score: no template"); return null; }
    var go=new GameObject("BounceStats Box Score",typeof(RectTransform)); go.SetActive(false); go.transform.SetParent(parent,false); var rt=(RectTransform)go.transform;
    rt.anchorMin=Vector2.zero; rt.anchorMax=Vector2.one; rt.offsetMin=rt.offsetMax=Vector2.zero;
    var dark=new GameObject("darkener",typeof(RectTransform),typeof(Image)); dark.transform.SetParent(go.transform,false); var dr=(RectTransform)dark.transform;
    dr.anchorMin=Vector2.zero; dr.anchorMax=Vector2.one; dr.offsetMin=dr.offsetMax=Vector2.zero; dark.GetComponent<Image>().color=new Color(0,0,0,0.82f);
    var page=Clone(tpl).transform; var b=new Box{go=go};
    foreach(var n in new[]{"bar","bar (1)"}){ var bar=Kid(page,n); if(bar!=null) bar.SetParent(go.transform,false); }
    var west=Kid(page,"WesternConference"); var east=Kid(page,"EasternConference");
    var tt=(GameObject)UnityEngine.Object.Instantiate(Kid(west,"ConferenceTitle").gameObject,go.transform); b.title=tt.GetComponent<TMP_Text>(); Place(tt.transform,0,530,1700,80);
    Fit(b.title,30,56); b.title.alignment=TextAlignmentOptions.Center; var le=tt.GetComponent<LayoutElement>(); if(le!=null) le.ignoreLayout=true;
    west.SetParent(go.transform,false); east.SetParent(go.transform,false); Place(west,60,445,1740,400); Place(east,60,10,1740,400);
    var cw=new float[]{110,110,110,110,110,160,160,110}; b.a=Table.From(west); b.a.Layout(420,cw,5); b.b=Table.From(east); b.b.Layout(420,cw,5);
    UnityEngine.Object.Destroy(page.gameObject);
    // The in-game UI camera only draws its own layer, and new objects start on Default.
    foreach(var x in go.GetComponentsInChildren<Transform>(true)) x.gameObject.layer=parent.gameObject.layer;
    var cv=go.AddComponent<Canvas>(); cv.overrideSorting=true; cv.sortingOrder=450; return b; }   // above the pause and end-of-match screens' own canvases
  static void RenderBox(Game g,string title){ Set(box.title,title+"   "+TeamName(g.a)+"  "+g.sa+"  -  "+g.sb+"  "+TeamName(g.b));
    var tabs=new[]{box.a,box.b}; var ids=new[]{g.a,g.b}; var sc=new[]{g.sa,g.sb};
    for(int s=0;s<2;s++){ var tb=tabs[s]; int t=ids[s]; Set(tb.title,TeamFull(t)+"   "+sc[s]); tb.Head("",LCols,-1);
      var ls=g.lines.Where(l=>l.team==t).OrderByDescending(l=>l.pts).ToList(); var tot=new Line(); int n=Math.Min(ls.Count,tb.rows.Count-1);
      for(int i=0;i<n;i++){ tb.Row(i,t,NameAny(ls[i].id),LCells(ls[i]),false,false); tot.Add(ls[i]); } tb.Row(n,t,"Team",LCells(tot),true,false); tb.Hide(n+1); } }
  // The screen underneath (pause menu or end of match) is faded out while the box score is up.
  static CanvasGroup under; static float underAlpha; static List<Renderer> hidden=new List<Renderer>();
  // The end screen shows award winners as the real 3D players, which draw over any UI, so they are hidden meanwhile.
  static void HideAwardPlayers(){ var aw=F(endUI,"m_aoAwards") as IList; if(aw==null)return;
    foreach(var pr in aw){ var c=F(pr,"First") as Component; if(c==null)continue; foreach(var x in c.GetComponentsInChildren<Renderer>()) if(x.enabled){ x.enabled=false; hidden.Add(x); } } }
  static void OpenBox(Transform parent,Game g,string title,GameObject ret,GameObject hide){ try{ if(g==null||parent==null)return;
      if(box==null||box.go==null||box.go.transform.parent!=parent){ if(box!=null&&box.go!=null) UnityEngine.Object.Destroy(box.go); box=MakeBox(parent); } if(box==null)return;
      box.go.transform.SetAsLastSibling(); box.go.SetActive(true); RenderBox(g,title);
      if(hide!=null){ under=hide.GetComponent<CanvasGroup>()??hide.AddComponent<CanvasGroup>(); underAlpha=under.alpha; under.alpha=0; }
      HideAwardPlayers(); boxOpen=true; boxOpened=Time.frameCount; boxReturn=ret;
      var es=EventSystem.current; if(es!=null) es.SetSelectedGameObject(null);
      module=F(Inst("bounce.RewiredManager"),"m_rewiredInputModule") as Behaviour; if(module!=null) module.enabled=false;
    }catch(Exception e){ L.LogError("Box score: "+e); } }
  static void CloseBox(){ boxOpen=false; boxClosed=Time.frameCount; if(under!=null) under.alpha=underAlpha; under=null; foreach(var x in hidden) if(x!=null) x.enabled=true; hidden.Clear(); if(box!=null&&box.go!=null) box.go.SetActive(false); if(module!=null) module.enabled=true; module=null;
    var es=EventSystem.current; if(es!=null&&boxReturn!=null&&boxReturn.activeInHierarchy) es.SetSelectedGameObject(boxReturn); }
  // While the box score is up the game's own menus must not see the same key presses.
  static bool Blocking(){ return boxOpen||Time.frameCount<=boxClosed+2; }
  static bool CheckPlayerPre(ref bool __result){ if(!Blocking())return true; __result=false; return false; }
  static bool BlockUpdatePre(){ return !Blocking(); }

  // A "Box Score" button cloned from one of the game's own menu buttons and placed right after it.
  static GameObject MakeButton(GameObject src,UnityEngine.Events.UnityAction click){ var parent=src.transform.parent; var go=Clone(src); go.name="BounceStats Box Score";
    var b=go.GetComponent<Button>(); for(int i=0;i<b.onClick.GetPersistentEventCount();i++) b.onClick.SetPersistentListenerState(i,UnityEngine.Events.UnityEventCallState.Off);
    b.onClick.RemoveAllListeners(); b.onClick.AddListener(click);
    var ic=Find(go.transform,"icon"); var sp=StatsIcon(); if(ic!=null&&sp!=null) ic.GetComponent<Image>().sprite=sp;
    go.transform.SetParent(parent,false); go.transform.SetSiblingIndex(src.transform.GetSiblingIndex()+1); go.SetActive(true);
    foreach(var t in go.GetComponentsInChildren<TMP_Text>(true)) Set(t,"Box Score"); return go; }
  static Sprite statsIcon;
  // The basketball the main menu uses for Pick-Up Game, which the pause menu's buttons share.
  static Sprite StatsIcon(){ if(statsIcon==null) statsIcon=Resources.FindObjectsOfTypeAll<Sprite>().FirstOrDefault(x=>x.name=="txt_ui_mainMenu_icon_quickMatch"); return statsIcon; }
  static GameObject pauseBtn, endBtn; static object hub, pause, endUI; static float endAt; static bool autoDone;
  static void PauseOn(object __instance){ pause=__instance; try{ if(pauseBtn!=null)return; var btns=F(__instance,"m_oPauseButtons") as Button[]; if(btns==null||btns.Length==0)return;
      var src=btns[0].gameObject; var parent=src.transform.parent; pauseBtn=MakeButton(src,()=>OpenBox(((Component)pause).transform.parent,LiveBox(),"Box Score",pauseBtn,((Component)pause).gameObject));
      var un=parent.GetComponent(T("bounce.UpdateNavigation")); var list=F(un,"m_aoSelectables") as IList;
      if(list!=null){ int at=list.IndexOf(btns[0].GetComponent<Selectable>()); list.Insert(at<0?list.Count:at+1,pauseBtn.GetComponent<Selectable>()); var m=AccessTools.Method(un.GetType(),"OnEnable"); if(m!=null) m.Invoke(un,null); }
    }catch(Exception e){L.LogError("Pause button: "+e);} }
  static void EndOn(object __instance){ endUI=__instance; endAt=Time.unscaledTime; autoDone=false; try{ if(endBtn!=null)return; var cont=F(__instance,"m_oButtonContinue") as GameObject; if(cont==null)return;
      endBtn=MakeButton(cont,()=>OpenBox(((Component)endUI).transform.parent.parent,lastBox??LiveBox(),"Final",endBtn,((Component)endUI).transform.parent.gameObject)); endBtn.SetActive(cont.activeSelf); }catch(Exception e){L.LogError("End button: "+e);} }

  static bool Gone(object o){ var b=o as Behaviour; return o!=null&&(b==null||!b.isActiveAndEnabled); }
  static bool Btn(string a,bool repeat){ try{ if(!Rewired.ReInput.isReady)return false; foreach(var p in Rewired.ReInput.players.GetPlayers()) if(repeat?p.GetButtonRepeating(a):p.GetButtonDown(a)) return true; }catch{} return false; }
  void Update(){
    if(dirty&&Time.unscaledTime>saveAt) SaveAll();
    // InGameUIPause has no OnDisable, so screens are dropped once they stop being active.
    if(Gone(hub))hub=null; if(Gone(pause))pause=null; if(Gone(endUI))endUI=null;
    if(boxOpen){ if(box==null||box.go==null||!box.go.activeInHierarchy||(pause==null&&endUI==null)){ CloseBox(); return; }
      if(Time.frameCount>boxOpened+1&&(Btn("UICancel",false)||Btn("UISubmit",false)||Btn("pause",false))) CloseBox(); return; }
    if(endUI!=null){ var cont=F(endUI,"m_oButtonContinue") as GameObject; bool show=cont!=null&&cont.activeSelf; if(endBtn!=null&&endBtn.activeSelf!=show) endBtn.SetActive(show);
      if(!autoDone&&AutoBox.Value&&lastWasSeason&&show&&Time.unscaledTime-endAt>1f&&!(bool)(F(endUI,"m_bShowingUnlockables")??false)){ autoDone=true; OpenBox(((Component)endUI).transform.parent.parent,lastBox,"Final",endBtn,((Component)endUI).transform.parent.gameObject); } }
    if(hub!=null) HubInput(); }
  void OnApplicationQuit(){ if(dirty)SaveAll(); }
}
