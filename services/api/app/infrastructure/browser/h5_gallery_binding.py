"""登记图库的数据属性配对；不执行自定义URL函数或探测任意框架内部对象。"""

H5_GALLERY_BINDING_CENSUS_SCRIPT = r"""
          const galleryPreviewItems=new WeakMap();
          const ownData=(owner,key)=>{
            const descriptor=owner && Object.getOwnPropertyDescriptor(owner,key);
            return descriptor && Object.hasOwn(descriptor,'value') ? descriptor.value : undefined;
          };
          const sourceGalleryBinding=image=>{
            if(!__H5_MODAL_CENSUS__ || !(image instanceof HTMLImageElement)) return null;
            try {
              let owner=image;
              for(let depth=0;owner && depth<8;depth++,owner=owner.parentElement) {
                const instance=ownData(owner,'viewer');
                if(!instance) continue;
                if(ownData(instance,'element')!==owner) return null;
                const images=ownData(instance,'images'),options=ownData(instance,'options');
                const attribute=ownData(options,'url');
                const size=ownData(images,'length');
                if(!Array.isArray(images) || !Number.isInteger(size) || size<1 || size>ELEMENT_LIMIT
                  || typeof attribute!=='string' || !/^data-[a-z0-9-]{1,40}$/.test(attribute))
                  return null;
                const indexes=[];
                for(let i=0;i<size;i++)if(ownData(images,String(i))===image)indexes.push(i);
                if(indexes.length!==1 || (owner!==image && !owner.contains(image)))return null;
                const raw=image.getAttribute(attribute);
                if(!raw || raw.length>URL_LIMIT) return null;
                const url=new URL(raw,document.baseURI);
                if(!['http:','https:'].includes(url.protocol) || url.href.length>URL_LIMIT)
                  return null;
                const binding={protocol:'viewerjs-data.v1',instance_id:previewId(instance),
                  source_index:indexes[0],attribute,resource:url.href};
                const root=ownData(instance,'viewer'),result=ownData(instance,'image');
                const index=ownData(instance,'index');
                if(root instanceof HTMLElement && probePreviewRoots.includes(root)
                  && result instanceof HTMLImageElement && root.contains(result)
                  && Number.isInteger(index) && index>=0 && index<size) {
                  galleryPreviewItems.set(result,{instance_id:binding.instance_id,
                    source_index:index,root:previewId(root)});
                }
                return binding;
              }
            } catch {return null;}
            return null;
          };
"""
