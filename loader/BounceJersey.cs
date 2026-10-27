using System; using System.IO; using System.Linq; using System.Text; using System.Reflection; using System.Collections.Generic;
using BepInEx; using BepInEx.Logging; using HarmonyLib; using UnityEngine; using TMPro;
// Bounce Jersey v2. The jersey layout (AvatarTexture.m_oMaster) is INACTIVE except inside
// AvatarTextureController.renderTexture(AvatarTexture), which SetActive(true)s it, enables the camera, cam.Render(), then deactivates it.
// It only runs when m_bRenderNeeded (avatar dirty). So: we build decorations in a PREFIX of that method (baked in the same Render),
// as plain MeshRenderers (no live TMP component), and call setRenderNeeded() when something changes.
[BepInPlugin("richvam.bounce.jersey","Bounce Jersey","2.4.1")]
public class BounceJersey : BaseUnityPlugin {
  const BindingFlags BF=BindingFlags.Instance|BindingFlags.Public|BindingFlags.NonPublic;
  const float BACK_NAME_Y=0.078f, BACK_NUM_DY=-0.05f, NAME_CAP=0.032f, FRONT_NUM_Y=0.30f, FRONT_NUM_SCALE=0.75f, NAME_W=0.15f, NAME_H=0.04f, ARCH=0.006f;
  class Opt{ public string name=""; public bool showName=true, frontNumber=true; public Color fill=Color.white, outline=new Color(0.08f,0.08f,0.08f,1); }
  static Dictionary<int,Opt> opts=new Dictionary<int,Opt>();
  static Dictionary<int,string> sig=new Dictionary<int,string>();   // avatar instance id -> signature of what was built
  static HashSet<int> diagDone=new HashSet<int>(); static ManualLogSource L; static TMP_FontAsset font; static BounceJersey inst; static BepInEx.Configuration.ConfigEntry<bool> Dbg; static bool D{get{return Dbg!=null&&Dbg.Value;}}
  void Awake(){ inst=this; L=Logger; Dbg=Config.Bind("Debug","Debug",false,"Write jersey_*.png debug dumps into the BepInEx folder and log [jersey] DIAG lines."); LoadOpts();
    try{ var h=new Harmony("richvam.bounce.jersey");
      var atc=AccessTools.TypeByName("AvatarTextureController"); var at=AccessTools.TypeByName("AvatarTexture"); var sa=AccessTools.TypeByName("SuperAvatar");
      var rt=AccessTools.Method(atc,"renderTexture",new[]{at});
      h.Patch(rt,prefix:new HarmonyMethod(typeof(BounceJersey),"PreRender"),postfix:new HarmonyMethod(typeof(BounceJersey),"PostRender"));
      foreach(var m in sa.GetMethods(BF).Where(m=>m.Name=="setPlayerNumber"&&m.DeclaringType==sa)) h.Patch(m,postfix:new HarmonyMethod(typeof(BounceJersey),"PostNumber"));
      L.LogInfo("[jersey] v2 patched "+rt+" + setPlayerNumber");
    }catch(Exception e){ L.LogError("[jersey] patch failed: "+e); } }
  static object F(object o,string n){ for(var t=o.GetType();t!=null;t=t.BaseType){var f=t.GetField(n,BF); if(f!=null)return f.GetValue(o);} return null; }
  static void S(object o,string n,object v){ for(var t=o.GetType();t!=null;t=t.BaseType){var f=t.GetField(n,BF); if(f!=null){f.SetValue(o,v);return;}} }
  static string[] Split(string s){ var o=new List<string>(); var sb=new StringBuilder(); bool q=false;
    for(int i=0;i<s.Length;i++){ char c=s[i]; if(q){ if(c=='"'){ if(i+1<s.Length&&s[i+1]=='"'){sb.Append('"');i++;} else q=false; } else sb.Append(c); }
      else if(c=='"')q=true; else if(c==','){o.Add(sb.ToString());sb.Length=0;} else sb.Append(c); } o.Add(sb.ToString()); return o.ToArray(); }
  void LoadOpts(){ opts.Clear(); try{
      var rp=Path.Combine(Paths.ConfigPath,"BounceRoster.csv"); if(File.Exists(rp)) foreach(var l in File.ReadAllLines(rp,Encoding.UTF8).Skip(1)){ var c=Split(l); int id;
        if(c.Length>1&&int.TryParse(c[0].Trim(),out id)&&id<=-1000) opts[id]=new Opt{name=c[1].Trim()}; }
      var jp=Path.Combine(Paths.ConfigPath,"BounceJerseys.csv"); if(File.Exists(jp)) foreach(var l in File.ReadAllLines(jp,Encoding.UTF8).Skip(1)){ var c=Split(l); int id; Opt o;
        if(c.Length<6||!int.TryParse(c[0].Trim(),out id)||!opts.TryGetValue(id,out o))continue;
        if(c[1].Trim().Length>0)o.name="\u0001"+c[1].Trim(); o.showName=c[2].Trim()!="0"; o.frontNumber=c[3].Trim()!="0";
        Color col; if(ColorUtility.TryParseHtmlString(c[4].Trim(),out col))o.fill=col; if(ColorUtility.TryParseHtmlString(c[5].Trim(),out col))o.outline=col; }
    }catch(Exception e){Logger.LogError("opts: "+e.Message);} Logger.LogInfo("[jersey] options for "+opts.Count+" custom players"); }
  static string BackName(string n){ if(n.StartsWith("\u0001"))return n.Substring(1).ToUpperInvariant(); var p=n.Trim().Split(' '); return p[p.Length-1].ToUpperInvariant(); }

  // number changed -> ask the game to re-render this avatar's textures (its own path)
  static void PostNumber(Component __instance){ try{ foreach(var c in __instance.GetComponentsInChildren(AccessTools.TypeByName("AvatarTextureController"),true)) RequestRender(c);
      foreach(MonoBehaviour c in Resources.FindObjectsOfTypeAll(AccessTools.TypeByName("AvatarTextureController"))) if(ReferenceEquals(F(c,"m_oAvatar"),__instance)) RequestRender(c);
    }catch(Exception e){ L.LogError("[jersey] PostNumber: "+e); } }
  static void RequestRender(Component c){ var m=c.GetType().GetMethod("setRenderNeeded",BF); if(m!=null&&m.GetParameters().Length==0) m.Invoke(c,null); }

  static string Sig(int id,MeshRenderer n,MeshRenderer n1,MeshRenderer n2){ var sb=new StringBuilder(); sb.Append(id);
    foreach(var r in new[]{n,n1,n2}){ if(r==null)continue; sb.Append('|').Append(r.enabled).Append(r.gameObject.activeSelf);
      var mt=r.sharedMaterial; if(mt!=null&&mt.HasProperty("_BaseMap")) sb.Append(mt.GetTextureOffset("_BaseMap").ToString("F3")); } return sb.ToString(); }

  // Runs right before the game's own SetActive(master)+cam.Render(); whatever we build here is baked into that render.
  static void PreRender(Component __instance, object __0){ try{
      if(Convert.ToBoolean(F(__0,"m_bFace")))return;
      var av=F(__instance,"m_oAvatar") as Component; if(av==null)return;
      var master=F(__0,"m_oMaster") as GameObject; var cam=F(__0,"m_oCamera") as Camera;
      var num=F(av,"m_oNumber") as MeshRenderer; if(num==null||master==null||!num.transform.IsChildOf(master.transform))return;
      var n1=F(av,"m_oNumber1") as MeshRenderer; var n2=F(av,"m_oNumber2") as MeshRenderer;
      int id=Convert.ToInt32(F(av,"m_iLastSetUniqueId")); int key=av.GetInstanceID(); var root=num.transform.parent;
      Opt op; bool custom=opts.TryGetValue(id,out op);
      string s=custom?Sig(id,num,n1,n2):"none"; string old; bool has=root.Cast<Transform>().Any(t=>t.name.StartsWith("bj_"));
      var backs=new List<Transform>{num.transform}; foreach(var r in new[]{n1,n2}){ if(r==null)continue; var t=r.transform; while(t.parent!=null&&t.parent!=root)t=t.parent; if(t.parent==root)backs.Add(t); }
      foreach(var t in backs) Restore(t);
      if(!(sig.TryGetValue(key,out old)&&old==s&&(has||!custom))){
        Clear(root); sig[key]=s;
        if(custom){ Build(av,root,num,n1,n2,id,op); if(D&&diagDone.Add(id)) Diag(id,master,cam,root,__instance); } }
      if(custom) foreach(var t in backs){ var lp=t.localPosition; var nl=new Vector3(lp.x,lp.y+BACK_NUM_DY,lp.z); shifted[t]=new KeyValuePair<Vector3,Vector3>(lp,nl); t.localPosition=nl; }
      if(custom&&shiftLog.Add(id)) L.LogInfo("[jersey] back number shifted for "+id+": "+string.Join(", ",backs.Select(t=>t.name+" y="+t.localPosition.y.ToString("F3")).ToArray()));
    }catch(Exception e){ L.LogError("[jersey] PreRender: "+e); } }

  static Dictionary<Transform,KeyValuePair<Vector3,Vector3>> shifted=new Dictionary<Transform,KeyValuePair<Vector3,Vector3>>(); static HashSet<int> shiftLog=new HashSet<int>();
  // put back the original position if it is still our shifted value (if the game moved it meanwhile, keep the game's value)
  static void Restore(Transform t){ KeyValuePair<Vector3,Vector3> kv; if(!shifted.TryGetValue(t,out kv))return; if((t.localPosition-kv.Value).sqrMagnitude<1e-8f) t.localPosition=kv.Key; shifted.Remove(t); }
  static void Clear(Transform root){ for(int i=root.childCount-1;i>=0;i--){ var c=root.GetChild(i); if(c.name.StartsWith("bj_")) DestroyImmediate(c.gameObject); } }

  static void Build(Component av,Transform root,MeshRenderer num,MeshRenderer n1,MeshRenderer n2,int id,Opt op){
    int layer=num.gameObject.layer; float z=num.transform.localPosition.z-0.004f;
    if(op.frontNumber){ float dy=FRONT_NUM_Y-num.transform.localPosition.y; var srcs=new List<Transform>();
      bool two=n1!=null&&n1.gameObject.activeSelf; 
      if(!two){ if(num.gameObject.activeSelf) srcs.Add(num.transform); }
      else foreach(var r in new[]{n1,n2}){ if(r==null)continue; var t=r.transform; while(t.parent!=null&&t.parent!=root) t=t.parent; if(t.parent==root) srcs.Add(t);
        L.LogInfo("[jersey] digit "+HPath(r.transform)+" active="+r.gameObject.activeInHierarchy+"/"+r.gameObject.activeSelf+" anchor="+t.name+" anchorActive="+t.gameObject.activeSelf+" lp="+t.localPosition.ToString("F3")+" mat="+(r.sharedMaterial!=null?r.sharedMaterial.name+" off="+r.sharedMaterial.GetTextureOffset("_BaseMap").ToString("F3"):"null")); }
      if(srcs.Count>0){ float cx=srcs.Average(t=>t.position.x); cx=root.InverseTransformPoint(new Vector3(cx,0,0)).x; cx=srcs.Average(t=>t.localPosition.x);
        foreach(var s in srcs){ var c=Instantiate(s.gameObject,root); c.name="bj_front_"+s.name; c.SetActive(true); var lp=s.localPosition;
          c.transform.localPosition=new Vector3(cx+(lp.x-cx)*FRONT_NUM_SCALE,lp.y+dy,lp.z); c.transform.localScale=s.localScale*FRONT_NUM_SCALE;
          var sr=s.GetComponentsInChildren<MeshRenderer>(true); var cr=c.GetComponentsInChildren<MeshRenderer>(true);
          for(int i=0;i<sr.Length&&i<cr.Length;i++){ cr[i].sharedMaterial=sr[i].sharedMaterial; cr[i].gameObject.SetActive(sr[i].gameObject.activeSelf); }
          foreach(var r in cr) L.LogInfo("[jersey] front clone "+HPath(r.transform)+" activeInHier="+r.gameObject.activeInHierarchy+" (master inactive pre-render is normal) wpos(local root)="+root.InverseTransformPoint(r.transform.position).ToString("F3")+" mat="+r.sharedMaterial.name); } }
      else L.LogWarning("[jersey] no front-number source found (two="+two+")"); }
    if(op.showName&&op.name.Length>0){
      var mesh=NameMesh(BackName(op.name),op); if(mesh==null)return; Rect r; var tex=NameTex(mesh,op,out r); if(tex==null)return;
      var go=new GameObject("bj_name"); go.layer=layer; go.transform.SetParent(root,false);
      go.transform.localPosition=new Vector3(num.transform.localPosition.x,BACK_NAME_Y,z); go.transform.localRotation=Quaternion.identity; go.transform.localScale=Vector3.one;
      var q=new Mesh(); q.name="bj_name_quad";
      q.vertices=new[]{new Vector3(r.xMin,r.yMin,0),new Vector3(r.xMin,r.yMax,0),new Vector3(r.xMax,r.yMax,0),new Vector3(r.xMax,r.yMin,0)};
      q.uv=new[]{new Vector2(0,0),new Vector2(0,1),new Vector2(1,1),new Vector2(1,0)}; q.triangles=new[]{0,1,2,0,2,3,0,2,1,0,3,2}; q.RecalculateNormals(); q.RecalculateBounds();
      go.AddComponent<MeshFilter>().sharedMesh=q; var mr=go.AddComponent<MeshRenderer>();
      var mat=new Material(num.sharedMaterial); mat.name="bj_name_mat"; mat.SetTexture("_BaseMap",tex); mat.SetTextureScale("_BaseMap",Vector2.one); mat.SetTextureOffset("_BaseMap",Vector2.zero);
      if(mat.HasProperty("_MainTex")){ mat.SetTexture("_MainTex",tex); mat.SetTextureScale("_MainTex",Vector2.one); mat.SetTextureOffset("_MainTex",Vector2.zero); }
      mat.SetColor("_BaseColor",Color.white); if(mat.HasProperty("_Color"))mat.SetColor("_Color",Color.white); mat.renderQueue=Math.Max(mat.renderQueue,3000)+1;
      mr.sharedMaterial=mat; mr.sortingOrder=num.sortingOrder+1; mr.shadowCastingMode=UnityEngine.Rendering.ShadowCastingMode.Off; mr.receiveShadows=false;
      L.LogInfo("[jersey] bj_name quad rect="+r+" tex="+tex.width+"x"+tex.height+" shader="+mat.shader.name+" kw=["+string.Join(",",mat.shaderKeywords)+"] q="+mat.renderQueue+" lp="+go.transform.localPosition.ToString("F3")); } }
  static float[] sdf; static int aw,ah; static bool atlasDumped;
  static bool LoadSdf(){ if(sdf!=null)return true; var a=font.atlasTexture; if(a==null){L.LogError("[jersey] font atlas null");return false;} aw=a.width; ah=a.height;
    var prev=RenderTexture.active; var rt=RenderTexture.GetTemporary(aw,ah,0,RenderTextureFormat.ARGB32,RenderTextureReadWrite.Linear);
    try{ Graphics.Blit(a,rt); RenderTexture.active=rt; var t=new Texture2D(aw,ah,TextureFormat.RGBA32,false,true); t.ReadPixels(new Rect(0,0,aw,ah),0,0); t.Apply(false);
      var px=t.GetPixels32(); int[] mn={255,255,255,255}, mx={0,0,0,0};
      foreach(var c in px){ int[] v={c.r,c.g,c.b,c.a}; for(int k=0;k<4;k++){ if(v[k]<mn[k])mn[k]=v[k]; if(v[k]>mx[k])mx[k]=v[k]; } }
      int ch=3; int best=-1; for(int k=3;k>=0;k--){ if(mx[k]-mn[k]>best){best=mx[k]-mn[k];ch=k;} }
      sdf=new float[px.Length]; for(int i=0;i<px.Length;i++){ var c=px[i]; int v=ch==0?c.r:ch==1?c.g:ch==2?c.b:c.a; sdf[i]=v/255f; }
      L.LogInfo("[jersey] atlas "+a.format+" "+aw+"x"+ah+" min RGBA="+string.Join("/",mn.Select(x=>x.ToString()).ToArray())+" max RGBA="+string.Join("/",mx.Select(x=>x.ToString()).ToArray())+" using channel "+"RGBA"[ch]);
      if(D&&!atlasDumped){ atlasDumped=true; var o=new Texture2D(aw,ah,TextureFormat.RGBA32,false); var op=new Color32[px.Length]; for(int i=0;i<px.Length;i++){ byte b=(byte)(sdf[i]*255); op[i]=new Color32(b,b,b,255); }
        o.SetPixels32(op); o.Apply(); File.WriteAllBytes(System.IO.Path.Combine(Paths.BepInExRootPath,"jersey_atlas.png"),o.EncodeToPNG()); Destroy(o); }
      Destroy(t);
    } finally { RenderTexture.active=prev; RenderTexture.ReleaseTemporary(rt); } return true; }
  static float Sample(float u,float v){ float x=u*aw-0.5f, y=v*ah-0.5f; int x0=Mathf.FloorToInt(x), y0=Mathf.FloorToInt(y); float fx=x-x0, fy=y-y0;
    Func<int,int,float> g=(xx,yy)=>{ xx=Mathf.Clamp(xx,0,aw-1); yy=Mathf.Clamp(yy,0,ah-1); return sdf[yy*aw+xx]; };
    return Mathf.Lerp(Mathf.Lerp(g(x0,y0),g(x0+1,y0),fx),Mathf.Lerp(g(x0,y0+1),g(x0+1,y0+1),fx),fy); }
  // CPU-render the arched glyph quads (SDF) into an RGBA texture: fill over outline, AA via smoothstep.
  static Texture2D NameTex(Mesh m,Opt op,out Rect r){ r=new Rect(); if(!LoadSdf())return null;
    var v=m.vertices; var uvl=new List<Vector4>(); m.GetUVs(0,uvl); var b=m.bounds; float pad=0.004f;
    r=Rect.MinMaxRect(b.min.x-pad,b.min.y-pad,b.max.x+pad,b.max.y+pad);
    float umax=uvl.Count>0?uvl.Max(u=>Mathf.Max(u.x,u.y)):0; bool pix=umax>1.5f;
    int W=512, H=Mathf.Clamp(Mathf.CeilToInt(W*r.height/r.width),16,512); var d=new float[W*H];
    for(int gq=0;gq+3<v.Length;gq+=4){ Vector2 p0=v[gq], p1=v[gq+1], p3=v[gq+3]; Vector2 t0=uvl[gq], t1=uvl[gq+1], t3=uvl[gq+3];
      if(pix){ t0=new Vector2(t0.x/aw,t0.y/ah); t1=new Vector2(t1.x/aw,t1.y/ah); t3=new Vector2(t3.x/aw,t3.y/ah); }
      Vector2 e1=p3-p0, e2=p1-p0; float l1=e1.sqrMagnitude, l2=e2.sqrMagnitude; if(l1<1e-12f||l2<1e-12f)continue;
      float mnx=Mathf.Min(v[gq].x,Mathf.Min(v[gq+1].x,Mathf.Min(v[gq+2].x,v[gq+3].x))), mxx=Mathf.Max(v[gq].x,Mathf.Max(v[gq+1].x,Mathf.Max(v[gq+2].x,v[gq+3].x)));
      float mny=Mathf.Min(v[gq].y,Mathf.Min(v[gq+1].y,Mathf.Min(v[gq+2].y,v[gq+3].y))), mxy=Mathf.Max(v[gq].y,Mathf.Max(v[gq+1].y,Mathf.Max(v[gq+2].y,v[gq+3].y)));
      int ix0=Mathf.Max(0,(int)((mnx-r.xMin)/r.width*W)-1), ix1=Mathf.Min(W-1,(int)((mxx-r.xMin)/r.width*W)+1);
      int iy0=Mathf.Max(0,(int)((mny-r.yMin)/r.height*H)-1), iy1=Mathf.Min(H-1,(int)((mxy-r.yMin)/r.height*H)+1);
      for(int y=iy0;y<=iy1;y++) for(int x=ix0;x<=ix1;x++){ var q=new Vector2(r.xMin+(x+0.5f)/W*r.width, r.yMin+(y+0.5f)/H*r.height)-p0;
        float s=Vector2.Dot(q,e1)/l1, t=Vector2.Dot(q,e2)/l2; if(s<0||s>1||t<0||t>1)continue;
        var uv=t0+s*(t3-t0)+t*(t1-t0); float val=Sample(uv.x,uv.y); if(val>d[y*W+x])d[y*W+x]=val; } }
    var px=new Color32[W*H]; Color fill=op.fill, outl=op.outline; float dmax=0;
    for(int i=0;i<px.Length;i++){ float dv=d[i]; dmax=Mathf.Max(dmax,dv); float fa=Mathf.SmoothStep(0,1,Mathf.InverseLerp(0.46f,0.54f,dv)); float oa=Mathf.SmoothStep(0,1,Mathf.InverseLerp(0.34f,0.42f,dv));
      var c=Color.Lerp(outl,fill,fa); c.a=oa; px[i]=c; }
    var tex=new Texture2D(W,H,TextureFormat.RGBA32,false); tex.name="bj_name_tex"; tex.wrapMode=TextureWrapMode.Clamp; tex.filterMode=FilterMode.Bilinear; tex.SetPixels32(px); tex.Apply(false,false);
    L.LogInfo("[jersey] name tex "+W+"x"+H+" uvMax="+umax.ToString("F3")+" pixelUV="+pix+" sdfMax="+dmax.ToString("F3"));
    if(D) try{ File.WriteAllBytes(System.IO.Path.Combine(Paths.BepInExRootPath,"jersey_nametex.png"),tex.EncodeToPNG()); }catch{}
    return tex; }
  static string HPath(Transform t){ var s=t.name; while(t.parent!=null){ t=t.parent; s=t.name+"/"+s; } return s; }
  static Texture2D white;
  // TMP atlas is Alpha8 (rgb sample = 0). Make an RGBA32 copy with rgb=1, a=SDF so URP/Unlit uses it directly.
  static Texture2D WhiteAtlas(){ if(white!=null)return white; var a=font.atlasTexture; if(a==null){L.LogError("[jersey] font atlas null");return null;}
    var prev=RenderTexture.active; var rt=RenderTexture.GetTemporary(a.width,a.height,0,RenderTextureFormat.ARGB32,RenderTextureReadWrite.Linear);
    try{ Graphics.Blit(a,rt); RenderTexture.active=rt; var t=new Texture2D(a.width,a.height,TextureFormat.RGBA32,false,true); t.ReadPixels(new Rect(0,0,a.width,a.height),0,0); 
      var px=t.GetPixels32(); int mxA=0,mxR=0; for(int i=0;i<px.Length;i++){ mxA=Math.Max(mxA,px[i].a); mxR=Math.Max(mxR,px[i].r); }
      bool useR=mxA==0||mxA==255&&mxR>0&&px.All(p=>p.a==255);   // some platforms put Alpha8 into .r
      for(int i=0;i<px.Length;i++){ byte v=useR?px[i].r:px[i].a; px[i]=new Color32(255,255,255,v); }
      t.SetPixels32(px); t.wrapMode=TextureWrapMode.Clamp; t.filterMode=FilterMode.Bilinear; t.Apply(false,false); t.name="bj_atlas_rgba"; white=t;
      L.LogInfo("[jersey] atlas format="+a.format+" "+a.width+"x"+a.height+" maxA="+mxA+" maxR="+mxR+" useR="+useR);
    } finally { RenderTexture.active=prev; RenderTexture.ReleaseTemporary(rt); } return white; }

  // One-time dump of the game's own baked jersey RT per custom id (read-only copy; never writes to the RT).
  static HashSet<int> dumped=new HashSet<int>();
  static void PostRender(Component __instance, object __0){ if(!D)return; try{
      if(Convert.ToBoolean(F(__0,"m_bFace")))return; var av=F(__instance,"m_oAvatar") as Component; if(av==null)return;
      int id=Convert.ToInt32(F(av,"m_iLastSetUniqueId")); if(!opts.ContainsKey(id)||!dumped.Add(id))return;
      var rt=F(__0,"m_oRenderTexture") as RenderTexture; if(rt==null)return; var prev=RenderTexture.active; RenderTexture.active=rt;
      var t=new Texture2D(rt.width,rt.height,TextureFormat.RGBA32,false); t.ReadPixels(new Rect(0,0,rt.width,rt.height),0,0); RenderTexture.active=prev;
      var f=System.IO.Path.Combine(Paths.BepInExRootPath,"jersey_"+(-id)+".png"); File.WriteAllBytes(f,t.EncodeToPNG()); Destroy(t);
      L.LogInfo("[jersey] dumped baked RT for "+id+" -> "+f);
    }catch(Exception e){ L.LogError("[jersey] PostRender: "+e); } }

  // Generate text geometry with a temporary, ACTIVE TextMeshPro (outside the layout and on an unused layer), copy + arch it into a plain Mesh.
  static Mesh NameMesh(string text,Opt op){
    if(font==null) font=Resources.FindObjectsOfTypeAll<TMP_FontAsset>().FirstOrDefault(f=>f.name=="lilita-one")??Resources.FindObjectsOfTypeAll<TMP_FontAsset>().FirstOrDefault();
    if(font==null){ L.LogError("[jersey] no TMP_FontAsset found"); return null; }
    var tmp=new GameObject("bj_tmp_gen"); tmp.layer=31; tmp.hideFlags=HideFlags.HideAndDontSave; tmp.transform.position=new Vector3(0,-10000,0);
    try{ var tm=tmp.AddComponent<TextMeshPro>(); tm.font=font; tm.fontSharedMaterial=font.material; tm.text=text; tm.color=op.fill; tm.alignment=TextAlignmentOptions.Center;
      tm.enableWordWrapping=false; tm.enableAutoSizing=false; tm.fontSize=1f; tm.characterSpacing=4; tm.color=Color.white;
      tm.rectTransform.sizeDelta=new Vector2(100f,10f); tm.ForceMeshUpdate(true,true);
      var src=tm.mesh; var ti=tm.textInfo;
      L.LogInfo("[jersey] TMP gen '"+text+"' font="+font.name+" atlas="+(font.atlasTexture!=null?font.atlasTexture.width+"x"+font.atlasTexture.height:"null")+" shader="+(font.material!=null?font.material.shader.name:"null")+
        " fontSize="+tm.fontSize.ToString("F3")+" chars="+ti.characterCount+" verts="+(src!=null?src.vertexCount:-1)+" bounds="+tm.bounds.size.ToString("F3"));
      if(src==null||src.vertexCount==0) return null;
      var v=src.vertices; float x0=1e9f,x1=-1e9f,cap=0,base0=0; int nv=0;
      for(int i=0;i<ti.characterCount;i++){ var ch=ti.characterInfo[i]; if(!ch.isVisible)continue; x0=Mathf.Min(x0,ch.bottomLeft.x); x1=Mathf.Max(x1,ch.topRight.x);
        cap=Mathf.Max(cap,ch.topLeft.y-ch.baseLine); base0+=ch.baseLine; nv++; }
      if(nv==0)return null; base0/=nv; float w=x1-x0; float sc=Mathf.Min(NAME_CAP/Mathf.Max(1e-5f,cap),NAME_W/Mathf.Max(1e-5f,w)); float mx=(x0+x1)*0.5f, my=base0+cap*0.5f;
      for(int i=0;i<v.Length;i++) v[i]=new Vector3((v[i].x-mx)*sc,(v[i].y-my)*sc,0);
      float half=Mathf.Max(0.0001f,w*sc*0.5f);
      L.LogInfo("[jersey] name '"+text+"' scaled: width="+(w*sc).ToString("F3")+" cap="+(cap*sc).ToString("F3")+" scale="+sc.ToString("F4"));
      for(int i=0;i<ti.characterCount;i++){ var ch=ti.characterInfo[i]; if(!ch.isVisible)continue; int b=ch.vertexIndex; if(b+3>=v.Length)continue;
        float cx=(v[b].x+v[b+2].x)*0.5f; float u=cx/half; float dy=ARCH*(1f-u*u); float ang=Mathf.Atan(2f*ARCH*u/half)*Mathf.Rad2Deg;
        var rot=Quaternion.Euler(0,0,-ang); /* perpendicular to arc: tops fan outward */ var ctr=new Vector3(cx,(v[b].y+v[b+1].y)*0.5f,0);
        for(int k=0;k<4;k++){ var p=v[b+k]-ctr; v[b+k]=rot*p+ctr+new Vector3(0,dy,0); } }
      var m=new Mesh(); m.name="bj_name_mesh"; m.vertices=v; m.colors32=Enumerable.Repeat(new Color32(255,255,255,255),v.Length).ToArray();
      var uv=new List<Vector4>(); src.GetUVs(0,uv); m.SetUVs(0,uv); var uv2=new List<Vector4>(); src.GetUVs(1,uv2); if(uv2.Count==v.Length)m.SetUVs(1,uv2);
      m.normals=src.normals; m.tangents=src.tangents; m.triangles=src.triangles; m.RecalculateBounds(); return m;
    } finally { DestroyImmediate(tmp); } }

  static void Diag(int id,GameObject master,Camera cam,Transform root,Component ctrl){
    var sb=new StringBuilder("[jersey] DIAG id="+id+" via AvatarTextureController.renderTexture ctrl="+ctrl.name+" master="+master.name+" masterActive="+master.activeSelf);
    if(cam!=null) sb.Append(" cam="+cam.name+" mask="+cam.cullingMask+" ortho="+cam.orthographicSize+" rt="+(cam.targetTexture!=null?cam.targetTexture.name+" "+cam.targetTexture.width:"null")+" camPos(local to root)="+root.InverseTransformPoint(cam.transform.position).ToString("F3")+" camFwd="+root.InverseTransformDirection(cam.transform.forward).ToString("F2"));
    L.LogInfo(sb.ToString());
    foreach(Transform t in root){ var r=t.GetComponent<Renderer>(); if(r==null)continue; var mt=r.sharedMaterial;
      L.LogInfo("[jersey]   "+t.name+" layer="+t.gameObject.layer+" inMask="+(cam!=null&&((cam.cullingMask>>t.gameObject.layer)&1)==1)+" en="+r.enabled+" activeSelf="+t.gameObject.activeSelf+
        " lp="+t.localPosition.ToString("F3")+" ls="+t.localScale.ToString("F3")+" bounds="+r.bounds.size.ToString("F3")+" mat="+(mt!=null?mt.name+"/"+mt.shader.name+" q="+mt.renderQueue:"null")+" sort="+r.sortingOrder); } }
}
