javascript:(async function(){
  /* 在聊天页 https://www.zhipin.com/web/chat/index 的 Console 运行
     会自动逐个打开编辑页 → 提取 → 返回 → 最终输出 JSON */

  const JOBS = [
    {v:"546545078", t:"开发 _ 广州 16-30K"},
    {v:"512180587", t:"线上获客与网红合作总监 _ 广州 10-15K"},
    {v:"512180562", t:"线上获客与网红合作总监 _ 广州 100-150元/天"},
    {v:"512180479", t:"CEO标注助理 _ 广州 10-15K"},
    {v:"511287189", t:"CEO标注助理 _ 广州 100-150元/天"},
    {v:"511287146", t:"CEO标注助理 _ 广州 100-150元/天"},
    {v:"511287075", t:"CEO标注助理 _ 广州 100-150元/天"},
    {v:"511286135", t:"线上获客与网红合作总监（关闭） _ 广州 100-150元/天"},
  ];

  const results = [];
  const total = JOBS.length;

  /* UI */
  const bar = document.createElement('div');
  bar.id = '_job_bar';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;background:#1a1a2e;color:#eee;padding:10px 20px;font:14px monospace';
  bar.innerHTML = `<b>提取岗位详情</b> <span id="_job_status"></span>`;
  document.body.appendChild(bar);

  function log(msg, c){
    const s = document.getElementById('_job_status');
    const color = {ok:'#4f8',err:'#f66'}[c]||'#8af';
    s.innerHTML += `<br><span style="color:${color}">${msg}</span>`;
  }

  /* 提取编辑页数据 */
  function extract(){
    const d = {title:'',requirements:'',salary:'',degree:'',location:''};
    document.querySelectorAll('input[type="text"], input:not([type])').forEach(el=>{
      const v=(el.value||'').trim();
      if(v&&/[一-鿿]/.test(v)&&v.length>2&&v.length<30&&!v.includes('zhipin')) d.title=d.title||v;
      if(v.length>8&&/[区路街大厦座层号]/.test(v)) d.location=v;
    });
    document.querySelectorAll('textarea').forEach(el=>{
      const v=(el.value||'').trim();
      if(v.length>50) d.requirements=v;
    });
    const km=document.body.innerText.match(/(\d{1,3})k/gi);
    if(km&&km.length>=2) d.salary=km[0].toUpperCase()+'-'+km[1].toUpperCase();
    const dm=document.body.innerText.match(/(博士|硕士|本科|大专)/);
    if(dm) d.degree=dm[1];
    return d;
  }

  function wait(ms){ return new Promise(r=>setTimeout(r,ms)); }
  function isEdit(){ return !!document.querySelector('textarea'); }
  function isList(){ return document.querySelectorAll('a').length>10 && !isEdit(); }

  for(let i=0;i<total;i++){
    const {v,t}=JOBS[i];
    const url=`https://www.zhipin.com/web/chat/job/edit?encryptId=${v}`;
    log(`[${i+1}/${total}] ${t}`, 'info');

    /* 导航 */
    window.location.href = url;
    /* 等编辑页渲染（最多15s） */
    for(let w=0;w<30;w++){
      await wait(500);
      if(isEdit()) break;
    }
    await wait(1500); /* 额外等表单填值 */

    const d = extract();
    d._id = v;
    d._text = t;
    results.push(d);
    log(`✓ ${d.title||t} | ${d.salary||'?'} | ${d.degree||'?'}`, 'ok');

    if(i<total-1){
      /* 返回聊天页 */
      window.location.href = 'https://www.zhipin.com/web/chat/index';
      for(let w=0;w<15;w++){ await wait(500); if(isList()) break; }
      await wait(2000+Math.random()*2000); /* 随机休息 */
    }
  }

  /* 输出 */
  const merged=[];
  const seen=new Set();
  for(const j of results){
    const key=`${j.title}|${j.salary}`;
    if(seen.has(key)) continue;
    seen.add(key);
    merged.push({title:j.title,requirements:(j.requirements||'').substring(0,800),salary:j.salary,degree:j.degree,location:j.location});
  }

  const out=JSON.stringify({extracted_at:new Date().toISOString(),jobs:merged},null,2);
  navigator.clipboard.writeText(out);
  const ta=document.createElement('textarea');
  ta.value=out;
  ta.style.cssText='position:fixed;top:10%;left:10%;z-index:100000;width:80%;height:80%;background:#fff;color:#111;font:12px monospace;padding:15px;border:2px solid #333;border-radius:6px';
  ta.onclick=function(){this.select()};
  document.body.appendChild(ta);
  setTimeout(()=>ta.select(),100);
  bar.innerHTML += `<br><b style="color:#4f8">✅ 完成! ${merged.length} 个岗位, JSON 已复制</b>`;
})();