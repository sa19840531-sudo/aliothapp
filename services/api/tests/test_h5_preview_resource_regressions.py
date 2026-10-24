"""真实浏览器最小复现与反例，不冒充原站高清预览验收。"""

import pytest

from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
from services.api.app.application.observation_packet import _compact_ui_snapshot
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.browser.page_capture import (
    BlockedPageCaptureActionError,
    _collect_ui_snapshot,
)
from services.api.tests.test_h5_probe_display_safety_regressions import IMAGE
from services.api.tests.test_h5_probe_journey_browser import execute
from services.api.tests.test_h5_probe_journey_browser import page as page


def test_absent_gallery_binding_preserves_previous_native_image_subject_key():
    image = {"selector": "#thumb", "tag": "img", "src": "https://media.invalid/small"}
    # 固定续修前的对象字段，防止新增空字段使旧原生图片重复补证。
    previous = {key: image.get(key) for key in (
        "selector", "tag", "role", "text", "src", "href", "aria_label", "alt",
        "class_tokens", "parent_class_tokens", "ancestor_class_tokens", "h5_media_instance_id",
        "h5_probe_scroll_context", "aria_expanded", "aria_controls",
        "h5_probe_preview_controlled_root", "h5_probe_image_resources",
    )}
    assert probe_subject_key(image) == stable_protocol_hash(previous)
    assert probe_subject_key({**image, "h5_probe_gallery_binding": None}) == stable_protocol_hash(
        previous)
    assert probe_subject_key({**image, "h5_probe_gallery_binding": {
        "protocol": "viewerjs-data.v1", "instance_id": "original", "source_index": 0,
        "resource": "https://media.invalid/large",
    }}) != stable_protocol_hash(previous)


@pytest.mark.parametrize(
    "bad", [None, "no_binding", "already_visible", "other_root", "duplicate_id", "new_and_old"]
)
async def test_reused_preview_requires_unique_control_binding_and_hidden_to_visible(page, bad):
    await page.set_content(
        '<div class="post-content"><img id="thumb" aria-controls="lightbox" '
        f'style="width:150px;height:100px" src="{IMAGE}"></div>'
        '<div id="lightbox" class="van-image-preview" style="display:none">'
        f'<img id="full" src="{IMAGE}" '
        'style="position:fixed;inset:0;width:390px;height:600px;z-index:9999"></div>'
    )
    await page.wait_for_function("thumb.complete && thumb.naturalWidth>0")
    await page.evaluate(
        """bad=>{
      if(bad==='no_binding')thumb.removeAttribute('aria-controls');
      if(bad==='already_visible'){lightbox.style.display='block';
        full.style='position:fixed;left:250px;top:400px;width:100px;height:100px';}
      if(bad==='other_root'||bad==='duplicate_id'){
        const other=lightbox.cloneNode(true);other.id=bad==='duplicate_id'?'lightbox':'other';
        other.querySelector('img').id='other_full';document.body.append(other);window.otherRoot=other;
      }
      window.clicks=0;thumb.onclick=()=>{window.clicks++;
        if(bad==='other_root')otherRoot.style.display='block';
        else {document.getElementById('lightbox').style.display='block';
          full.style='position:fixed;inset:0;width:390px;height:600px;z-index:9999';}}
      if(bad==='new_and_old'){const click=thumb.onclick;thumb.onclick=()=>{
        click();const clone=document.getElementById('lightbox').cloneNode(true);
        clone.id='new';clone.querySelector('img').id='new_full';document.body.append(clone);
      }}
    }""",
        bad,
    )
    trace, before, after = await execute(page, "media_fullscreen_pair", "post_list")
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is (bad is None)
    assert await page.evaluate("window.clicks") == 1
    if bad is None:
        assert before != after
        effect = trace[-1]["state_diff"]["h5_probe_effect"]
        assert effect["before_selector"] != effect["after_selector"]
        assert effect["preview_root_id"]


@pytest.mark.parametrize("bad", [None, "unlisted", "different_host", "changed_family", "truncated"])
async def test_native_srcset_binds_high_resolution_without_guessing_url_similarity(page, bad):
    async def images(route):
        await route.fulfill(
            content_type="image/svg+xml",
            body=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800">'
                '<rect width="1200" height="800" fill="red"/></svg>'
            ),
        )

    await page.context.route("https://*.invalid/**", images)
    await page.set_content(
        '<div class="post-content"><img id="thumb" style="width:150px;height:100px" '
        'src="https://media.invalid/small" sizes="150px" '
        'srcset="https://media.invalid/small 150w, https://media.invalid/large 1200w"></div>'
    )
    await page.wait_for_function("thumb.complete && thumb.naturalWidth>0")
    raw = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    compact = _compact_ui_snapshot(raw)
    assert probe_subject_key(
        select_probe_target(raw, goal="media_fullscreen_pair", page_type="post_list")
    ) == probe_subject_key(
        select_probe_target(compact, goal="media_fullscreen_pair", page_type="post_list")
    )
    await page.evaluate(
        """bad=>{
      if(bad==='truncated')thumb.srcset=Array.from({length:20},(_,i)=>
        `https://media.invalid/large${i} ${i+1}x`).join(',');
      window.clicks=0;thumb.onclick=()=>{window.clicks++;
        const root=document.createElement('div');root.className='van-image-preview';
        const img=document.createElement('img');img.id='full';
        img.style='position:fixed;inset:0;width:390px;height:600px;z-index:9999';
        img.src=bad==='unlisted'?'https://media.invalid/large?another=1':
          bad==='different_host'?'https://other.invalid/large':'https://media.invalid/large';
        root.append(img);document.body.append(root);
        if(bad==='changed_family')thumb.srcset='https://media.invalid/small 150w';
      };
    }""",
        bad,
    )
    if bad == "truncated":
        await page.wait_for_function("thumb.complete && thumb.naturalWidth>0")
    trace, _, _ = await execute(page, "media_fullscreen_pair", "post_list")
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is (bad is None)
    assert await page.evaluate("window.clicks") == 1
    if bad is None:
        effect = trace[-1]["state_diff"]["h5_probe_effect"]
        assert effect["before_resource"] == "https://media.invalid/small"
        assert effect["after_resource"] == "https://media.invalid/large"


async def test_variant_binding_survives_observer_and_rejects_pre_input_drift(page):
    await page.set_content(
        '<div class="post-content"><img id="thumb" style="width:150px;height:100px" '
        f'src="{IMAGE}"></div>'
    )
    await page.wait_for_function("thumb.complete && thumb.naturalWidth>0")
    raw = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    compact = _compact_ui_snapshot(raw)
    source = select_probe_target(raw, goal="media_fullscreen_pair", page_type="post_list")
    saved = select_probe_target(compact, goal="media_fullscreen_pair", page_type="post_list")
    assert probe_subject_key(source) == probe_subject_key(saved)
    assert "h5_probe_image_resources" in saved

    async def mutate():
        await page.evaluate("""()=>{
          window.clicks=0;thumb.onclick=()=>window.clicks++;
          thumb.srcset='https://media.invalid/other 2x';
        }""")

    with pytest.raises(BlockedPageCaptureActionError, match="漂移"):
        await execute(page, "media_fullscreen_pair", "post_list", before_execute=mutate)
    assert await page.evaluate("window.clicks") == 0


@pytest.mark.parametrize("wrong_branch", [False, True])
async def test_picture_pairs_selected_family_not_inactive_art_direction(page, wrong_branch):
    async def images(route):
        await route.fulfill(
            content_type="image/svg+xml",
            body=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800">'
                '<rect width="1200" height="800" fill="red"/></svg>'
            ),
        )

    await page.context.route("https://media.invalid/**", images)
    await page.set_content("""<div class="post-content"><picture>
      <source media="(min-width:1000px)" srcset="https://media.invalid/other 2x">
      <source media="(max-width:999px)" sizes="150px"
        srcset="https://media.invalid/small 150w, https://media.invalid/large 1200w">
      <img id="thumb" src="https://media.invalid/fallback" style="width:150px;height:100px">
      </picture></div>""")
    await page.wait_for_function("thumb.complete && thumb.naturalWidth>0")
    await page.evaluate(
        """wrong=>{thumb.onclick=()=>{
      const root=document.createElement('div');root.className='van-image-preview';
      const image=document.createElement('img');image.id='full';
      image.src=wrong?'https://media.invalid/other':'https://media.invalid/large';
      image.style='position:fixed;inset:0;width:390px;height:600px;z-index:9999';
      root.append(image);document.body.append(root);
    }}""",
        wrong_branch,
    )
    trace, _, _ = await execute(page, "media_fullscreen_pair", "post_list")
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is not wrong_branch


async def test_identical_pixels_do_not_make_replaced_preview_roots_stable(page, monkeypatch):
    from services.api.app.infrastructure.browser import page_capture

    await page.set_content(
        '<div class="post-content"><img id="thumb" style="width:150px;height:100px" '
        f'src="{IMAGE}"></div>'
    )
    await page.wait_for_function("thumb.complete && thumb.naturalWidth>0")
    await page.evaluate("""()=>{window.clicks=0;thumb.onclick=()=>{
      window.clicks++;const root=document.createElement('div');root.id='preview_root';
      root.className='van-image-preview';const image=thumb.cloneNode();image.id='full';
      image.style='position:fixed;inset:0;width:390px;height:600px;z-index:9999';
      root.append(image);document.body.append(root);
    }}""")
    collect = page_capture._collect_ui_snapshot

    async def replacing_dom(page, **kwargs):
        # 每次后帧前重建实际节点，确定性复现动画框架复用选择器但更换预览实例。
        await page.evaluate("""()=>{
          const root=document.getElementById('preview_root');
          if(root)root.replaceWith(root.cloneNode(true));
        }""")
        return await collect(page, **kwargs)

    monkeypatch.setattr(page_capture, "_collect_ui_snapshot", replacing_dom)
    trace, _, _ = await execute(page, "media_fullscreen_pair", "post_list")
    assert trace[-1]["result"] == "probe_effect_unverified"
    assert await page.evaluate("window.clicks") == 1


@pytest.mark.parametrize("bad", [None, "wrong_index", "wrong_image", "changed_url", "no_owner",
                               "function_url", "getter", "wrong_owner", "oversized",
                               "boolean_index", "fractional_index", "lazy_root",
                               "pre_input_instance", "pre_input_resource",
                               "wrong_index_same_source", "unowned_same_source", "same_source"])
async def test_registered_gallery_pairs_only_explicit_source_index_and_resource(page, bad):
    async def images(route):
        await route.fulfill(content_type="image/svg+xml", body=(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800">'
            '<rect width="1200" height="800" fill="red"/></svg>'
        ))

    await page.context.route("https://media.invalid/**", images)
    await page.set_content(
        '<div id="gallery" class="post-content"><img id="thumb" '
        'src="https://media.invalid/small" data-original="https://media.invalid/large" '
        'style="width:150px;height:100px"></div>'
        '<div id="lightbox" class="viewer-container" style="display:none"></div>'
    )
    await page.wait_for_function("thumb.complete && thumb.naturalWidth>0")
    await page.evaluate("""bad=>{
      window.clicks=0;window.getters=0;
      const state={element:gallery,images:[thumb],options:{url:'data-original'},
        viewer:lightbox,index:0,image:null};gallery.viewer=state;
      if(bad==='no_owner')delete gallery.viewer;
      if(bad==='function_url')state.options.url=()=>{
        window.getters++;return thumb.dataset.original;};
      if(bad==='getter')Object.defineProperty(gallery,'viewer',{
        get(){window.getters++;return state;}});
      if(bad==='wrong_owner')state.element=document.body;
      if(bad==='oversized')state.images=Array(301).fill(thumb);
      if(['wrong_index_same_source','unowned_same_source','same_source'].includes(bad))
        thumb.dataset.original=thumb.src;
      if(bad==='lazy_root')state.viewer=null;
      thumb.onclick=()=>{window.clicks++;
        const image=document.createElement('img');image.id='full';
        image.src=bad==='wrong_image'?'https://media.invalid/another':thumb.dataset.original;
        if(bad==='wrong_index_same_source'||bad==='unowned_same_source')image.src=thumb.src;
        image.style='position:fixed;inset:0;width:390px;height:600px;z-index:9999';
        let root=lightbox;
        if(bad==='lazy_root'){root=document.createElement('div');
          root.className='viewer-container';document.body.append(root);state.viewer=root;}
        root.append(image);root.style.display='block';state.image=image;
        if(bad==='wrong_index')state.index=1;
        if(bad==='wrong_index_same_source')state.index=1;
        if(bad==='unowned_same_source')state.image=null;
        if(bad==='boolean_index')state.index=false;
        if(bad==='fractional_index')state.index=0.5;
        if(bad==='changed_url')thumb.dataset.original='https://media.invalid/changed';
      };
    }""", bad)
    raw = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    compact = _compact_ui_snapshot(raw)
    assert probe_subject_key(select_probe_target(raw, goal="media_fullscreen_pair",
                                                page_type="post_list")) == probe_subject_key(
        select_probe_target(compact, goal="media_fullscreen_pair", page_type="post_list"))
    if bad in {"pre_input_instance", "pre_input_resource"}:
        async def mutate():
            await page.evaluate("""bad=>{
              if(bad==='pre_input_instance')gallery.viewer={...gallery.viewer};
              else thumb.dataset.original='https://media.invalid/changed';
            }""", bad)

        with pytest.raises(BlockedPageCaptureActionError, match="漂移"):
            await execute(page, "media_fullscreen_pair", "post_list", before_execute=mutate)
        assert await page.evaluate("window.clicks") == 0
        return
    trace, _, _ = await execute(page, "media_fullscreen_pair", "post_list")
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is (
        bad in {None, "lazy_root", "same_source"})
    assert await page.evaluate("window.clicks") == 1
    assert await page.evaluate("window.getters") == 0
