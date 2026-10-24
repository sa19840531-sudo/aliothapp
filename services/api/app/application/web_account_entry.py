"""视觉无法识别时，用当前页面可见语义补定位 Web 账户入口。"""


async def unique_account_entry(browser):
    """只返回唯一、可点击且站内的账户入口候选；否则保持未知。"""
    page = getattr(browser, "page", None)
    if page is None:
        return None
    try:
        return await page.evaluate(
            r"""() => {
              const account = new RegExp(
                'account|profile|user|member|login|log-in|sign-in|signin|' +
                'register|signup|sign-up|账户|账号|登录|登入|注册|註冊|会员|个人|我的', 'i');
              const distractor = /advert|promo|banner|download|share|search|广告|下载|搜索|分享/i;
              const visibleText = new RegExp(
                '登录|登入|注册|註冊|账户|账号|个人中心|会员中心|我的账户|' +
                'sign[ -]?in|log[ -]?in|register|sign[ -]?up|my account|profile', 'i');
              const candidates = [...document.querySelectorAll(
                'a,button,[role="button"]')].map(e => {
                  const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                  if (r.width < 16 || r.height < 16 ||
                      r.width * r.height / (innerWidth * innerHeight) > 0.08 ||
                      r.left < 0 || r.top < 0 || r.right > innerWidth ||
                      r.bottom > innerHeight || s.display === 'none' ||
                      s.visibility === 'hidden' || Number(s.opacity) === 0 ||
                      s.pointerEvents === 'none') return null;
                  const top = document.elementFromPoint(
                    r.left + r.width / 2, r.top + r.height / 2);
                  if (!top || !(top === e || e.contains(top) || top.contains(e)))
                    return null;
                  const href = e.getAttribute('href');
                  if (href && !href.startsWith('#') &&
                      !/^javascript:(?:;|void\(0\);?)$/i.test(href.trim())) {
                    try {
                      if (new URL(href, location.href).origin !== location.origin)
                        return null;
                    } catch { return null; }
                  }
                  const label = (e.innerText || e.getAttribute('aria-label') ||
                    e.getAttribute('title') || '').trim().replace(/\s+/g, ' ');
                  const structural = [e.className, e.id,
                    ...[...e.querySelectorAll('img')].map(img =>
                      [img.className, img.alt].filter(Boolean).join(' '))]
                    .filter(Boolean).join(' ');
                  if (distractor.test(label + ' ' + structural)) return null;
                  const textEntry = visibleText.test(label);
                  const brokenIcon = account.test(structural) &&
                    [...e.querySelectorAll('img')].some(img =>
                      img.complete && img.naturalWidth === 0);
                  const headerIcon = account.test(structural) &&
                    (e.closest('header,nav,[role="navigation"]') ||
                     r.left >= innerWidth * 0.55 && r.top <= innerHeight * 0.30) &&
                    (e.querySelector('svg,img') || /user|account|profile/i.test(
                      [e.className,e.id].join(' ')));
                  if (!textEntry && !brokenIcon && !headerIcon) return null;
                  for (let p = e.parentElement, depth = 0;
                       p && depth < 3; p = p.parentElement, depth++) {
                    if (distractor.test(String(p.className || ''))) return null;
                  }
                  return {element:e, box:[r.left/innerWidth,r.top/innerHeight,
                    r.right/innerWidth,r.bottom/innerHeight],
                    label,
                    basis:textEntry ? 'visible_account_text' :
                      brokenIcon ? 'broken_account_icon' : 'header_account_icon'};
                }).filter(Boolean);
              const leaves = candidates.filter(item => !candidates.some(other =>
                other !== item && item.element.contains(other.element)));
              const directLogin = leaves.filter(item =>
                /^(登录|登入|登錄|log[ -]?in|sign[ -]?in)$/i.test(item.label) &&
                !item.element.closest('form'));
              if (directLogin.length === 1 && leaves.some(item =>
                  /^(注册|註冊|register|sign[ -]?up)$/i.test(item.label)))
                return {box:directLogin[0].box, basis:'adjacent_login_register_entry'};
              if (leaves.length === 2) {
                const login = leaves.filter(item =>
                  /^(登录|登入|log[ -]?in|sign[ -]?in)$/i.test(item.label));
                const register = leaves.filter(item =>
                  /^(注册|註冊|register|sign[ -]?up)$/i.test(item.label));
                if (login.length === 1 && register.length === 1 &&
                    login[0].element.parentElement === register[0].element.parentElement &&
                    !login[0].element.closest('form') &&
                    !register[0].element.closest('form')) {
                  return {box:login[0].box, basis:'adjacent_login_register_entry'};
                }
              }
              return leaves.length === 1 ? {box:leaves[0].box,
                basis:leaves[0].basis} : null;
            }"""
        )
    except Exception:
        return None


async def unique_authenticated_account_entry(browser):
    """登录后只在页头定位唯一、无遮挡、同源的个人资料入口。"""
    page = getattr(browser, "page", None)
    if page is None:
        return None
    try:
        return await page.evaluate(
            r"""() => {
              const candidates=[...document.querySelectorAll('a[href]')].map(e=>{
                let target;
                try { target=new URL(e.getAttribute('href'),location.href); }
                catch { return null; }
                if(target.origin!==location.origin ||target.search||target.hash||
                   e.closest('main,article,[class*="card"]')||
                   !/(?:^|\/)(?:user|account|member)\/profile\/?$|^\/profile\/?$/i
                     .test(target.pathname)) return null;
                const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                if(r.width<16||r.height<16||r.left<innerWidth*.55||
                   r.top<0||r.top>innerHeight*.3||r.right>innerWidth||
                   r.bottom>innerHeight||s.display==='none'||
                   s.visibility==='hidden'||Number(s.opacity)===0||
                   s.pointerEvents==='none') return null;
                const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                if(!(hit===e||e.contains(hit))) return null;
                return {box:[r.left/innerWidth,r.top/innerHeight,
                  r.right/innerWidth,r.bottom/innerHeight]};
              }).filter(Boolean);
              if(candidates.length) return candidates.length===1 ? candidates[0] : null;
              const globalMy=[...document.querySelectorAll(
                'nav a[href],aside a[href],[role="navigation"] a[href]')]
                .map(e=>{
                  if(e.closest('main,article,[class*="card"]')) return null;
                  const label=(e.innerText||e.getAttribute('aria-label')||'')
                    .trim().replace(/\s+/g,' ');
                  if(!/^(?:我的|个人中心|我的账户|my account|profile)$/i.test(label))
                    return null;
                  let target;
                  try { target=new URL(e.getAttribute('href'),location.href); }
                  catch { return null; }
                  if(target.origin!==location.origin||target.search||target.hash||
                     !/^\/(?:my|user\/profile|account\/profile|member\/profile|profile)\/?$/i
                       .test(target.pathname)) return null;
                  const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                  if(r.width<16||r.height<16||r.left<0||r.top<0||
                     r.right>innerWidth||r.bottom>innerHeight||
                     s.display==='none'||s.visibility==='hidden'||
                     Number(s.opacity)===0||s.pointerEvents==='none') return null;
                  const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                  if(!(hit===e||e.contains(hit))) return null;
                  return {box:[r.left/innerWidth,r.top/innerHeight,
                    r.right/innerWidth,r.bottom/innerHeight]};
                }).filter(Boolean);
              return globalMy.length===1 ? globalMy[0] : null;
            }"""
        )
    except Exception:
        return None


async def unique_register_entry(browser):
    """在当前可见账户弹层中定位唯一注册切换控件，不把注册提交按钮当入口。"""
    page = getattr(browser, "page", None)
    if page is None:
        return None
    try:
        return await page.evaluate(
            """() => {
              const register = /^(?:注册|註冊|register|sign[ -]?up)$/i;
              const registerLead = /^(?:注册|註冊|register\b|sign[ -]?up\b)/i;
              const login = /^(?:登录|登入|log[ -]?in|sign[ -]?in)$/i;
              const controls = [...document.querySelectorAll(
                'button,a,[role="button"],[role="tab"]')];
              const visible = e => {
                const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                if (r.width < 16 || r.height < 16 || r.right <= 0 || r.bottom <= 0 ||
                    r.left >= innerWidth || r.top >= innerHeight || s.display === 'none' ||
                    s.visibility === 'hidden' || Number(s.opacity) === 0 ||
                    s.pointerEvents === 'none') return null;
                const hit = document.elementFromPoint(r.left + r.width/2, r.top + r.height/2);
                if (!hit || !(hit === e || e.contains(hit))) return null;
                return r;
              };
              const label = e => (e.innerText || e.getAttribute('aria-label') || '')
                .trim().replace(/\\s+/g, ' ');
              const candidates = controls.filter(e => register.test(label(e)) &&
                e.getAttribute('type') !== 'submit').map(e => ({e, r:visible(e)}))
                .filter(item => item.r && controls.some(other =>
                  other !== item.e && login.test(label(other)) && visible(other) &&
                  (other.parentElement === item.e.parentElement ||
                   other.getAttribute('role') === 'tab' &&
                   item.e.getAttribute('role') === 'tab')));
              const modalCandidates = controls.filter(e => {
                if (e.getAttribute('type') === 'submit' ||
                    e.tagName === 'BUTTON' && e.type === 'submit' ||
                    !(registerLead.test(label(e)) ||
                      /(?:register|signup|sign-up|注册|註冊)/i.test(
                        [e.className,e.id,e.getAttribute('aria-label'),
                         e.getAttribute('title')].join(' ')))) return false;
                let layer=e.parentElement;
                while (layer && layer !== document.body &&
                       getComputedStyle(layer).position !== 'fixed' &&
                       !layer.matches('dialog,[aria-modal="true"]'))
                  layer=layer.parentElement;
                return layer && layer !== document.body &&
                  [...layer.querySelectorAll('input[type="password"]')]
                    .some(input => visible(input));
              }).map(e => ({e, r:visible(e)})).filter(item => item.r);
              const selected = candidates.length === 1 ? candidates :
                modalCandidates.length === 1 ? modalCandidates : [];
              if (selected.length !== 1) return null;
              const r = selected[0].r;
              return {box:[r.left/innerWidth,r.top/innerHeight,
                r.right/innerWidth,r.bottom/innerHeight],
                basis:selected === candidates ? 'register_tab' : 'modal_register_control'};
            }"""
        )
    except Exception:
        return None


async def active_auth_mode(browser):
    """只在可见登录/注册标签具有明确选中状态时判断当前表单模式。"""
    page = getattr(browser, "page", None)
    if page is None:
        return None
    try:
        return await page.evaluate(
            """() => {
              const tabs = [...document.querySelectorAll('[role="tab"]')]
                .filter(e => {
                  const r=e.getBoundingClientRect(), s=getComputedStyle(e);
                  return r.width>0 && r.height>0 && s.display!=='none' &&
                    s.visibility!=='hidden' && Number(s.opacity)!==0;
                });
              const label=e=>(e.innerText||'').trim().replace(/\\s+/g,' ');
              const login=tabs.filter(e=>/^(登录|登入|log[ -]?in|sign[ -]?in)$/i.test(label(e)));
              const register=tabs.filter(e=>/^(注册|註冊|register|sign[ -]?up)$/i.test(label(e)));
              if(login.length!==1 || register.length!==1) return null;
              const selected=e=>e.getAttribute('aria-selected')==='true' ||
                /(?:^|[\\s_-])(?:is-active|active|selected)(?:$|[\\s_-])/i
                  .test(String(e.className||''));
              if(selected(login[0])===selected(register[0])) return null;
              return selected(register[0]) ? 'register' : 'login';
            }"""
        )
    except Exception:
        return None


async def visible_login_form(browser):
    """无标签弹窗也需以唯一密码框和同层登录提交控件证明当前为登录表单。"""
    page = getattr(browser, "page", None)
    if page is None:
        return False
    try:
        return await page.evaluate(
            """() => {
              // TABLESS_LOGIN_FORM：只接受当前最上层弹窗中的单密码登录表单。
              const visible=e=>{
                const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                if(r.width<16||r.height<16||r.left<0||r.top<0||
                   r.right>innerWidth||r.bottom>innerHeight||
                   s.display==='none'||s.visibility==='hidden'||
                   Number(s.opacity)===0||s.pointerEvents==='none') return false;
                const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                return hit===e||e.contains(hit);
              };
              const passwords=[...document.querySelectorAll('input[type="password"]')]
                .filter(visible);
              if(passwords.length!==1) return false;
              const label=e=>(e.innerText||e.value||e.getAttribute('aria-label')||'')
                .trim().replace(/\\s+/g,' ');
              for(let layer=passwords[0].parentElement,depth=0;
                  layer&&layer!==document.body&&depth<9;
                  layer=layer.parentElement,depth++) {
                const style=getComputedStyle(layer);
                if(!layer.matches('dialog,[aria-modal="true"]')&&
                   !['fixed','absolute'].includes(style.position)) continue;
                const controls=[...layer.querySelectorAll(
                  'button,input[type="submit"],[role="button"]')].filter(visible);
                const login=controls.filter(e=>
                  /^(?:登录|登入|log[ -]?in|sign[ -]?in)$/i.test(label(e)));
                const register=controls.filter(e=>
                  /^(?:注册|註冊|register|sign[ -]?up)$/i.test(label(e)));
                if(login.length===1) return register.length===0;
                if(register.length>0) return false;
              }
              return false;
            }"""
        )
    except Exception:
        return False
