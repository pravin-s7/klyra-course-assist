// ── Markdown, Asset URL Normalization & Image Lightbox Module ─────

function resolveAssetUrl(rawUrl, baseUrl = '') {
    if (!rawUrl) return rawUrl;
    let url = rawUrl.trim();
    if (/^(https?:|data:|blob:)/i.test(url)) return url;

    let subject = '';
    const baseMatch = (baseUrl || '').match(/^\/files\/([^/]+)\//);
    if (baseMatch) {
        subject = baseMatch[1];
    }

    // 1. 'docs/<subject>/.assets/...' or 'docs/<subject>/assets/...'
    const docsMatch = url.match(/^\/?docs\/([^/]+)\/\.?assets\/(.+)$/);
    if (docsMatch) {
        return `/files/${docsMatch[1]}/.assets/${docsMatch[2]}`;
    }

    // 2. '/files/<subject>/assets/...' (missing dot on .assets)
    const filesAssetsMatch = url.match(/^\/files\/([^/]+)\/assets\/(.+)$/);
    if (filesAssetsMatch) {
        return `/files/${filesAssetsMatch[1]}/.assets/${filesAssetsMatch[2]}`;
    }

    // 3. Relative '../.assets/...', './.assets/...', '.assets/...', or '../assets/...'
    const relAssetsMatch = url.match(/^(?:\.\.\/|\.\/)*\.?assets\/(.+)$/);
    if (relAssetsMatch) {
        if (subject) {
            return `/files/${subject}/.assets/${relAssetsMatch[1]}`;
        }
        return `/.assets/${relAssetsMatch[1]}`;
    }

    // 4. '<subject>/.assets/...' without leading '/files/'
    const subjAssetsMatch = url.match(/^([a-zA-Z0-9_-]+)\/\.assets\/(.+)$/);
    if (subjAssetsMatch) {
        return `/files/${subjAssetsMatch[1]}/.assets/${subjAssetsMatch[2]}`;
    }

    return url;
}

function normalizeMarkdownImages(text, baseUrl = '') {
    if (!text) return '';
    let inferredBase = baseUrl;
    if (!inferredBase) {
        const subjMatch = text.match(/\/files\/([a-zA-Z0-9_-]+)\/|docs\/([a-zA-Z0-9_-]+)\//);
        if (subjMatch) {
            const s = subjMatch[1] || subjMatch[2];
            inferredBase = `/files/${s}/`;
        }
    }

    // 1. Rewrite ![alt](url) image paths
    let out = text.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, rawUrl) => {
        const resolved = resolveAssetUrl(rawUrl, inferredBase);
        return `![${alt}](${resolved})`;
    });

    // 2. Convert non-image markdown links [label](url.png) that point to image files into inline images
    out = out.replace(/(?<!!)\[([^\]]+)\]\(([^)]+\.(?:png|jpg|jpeg|gif|svg|webp)(?:\?[^)]*)?)\)/gi, (match, label, rawUrl) => {
        const resolved = resolveAssetUrl(rawUrl, inferredBase);
        return `![${label}](${resolved})`;
    });

    // 3. Rewrite markdown links pointing to docs/<subject>/... into /api/download/<subject>/...
    out = out.replace(/(?<!!)\[([^\]]+)\]\(\/?docs\/([a-zA-Z0-9_-]+)\/([^)]+)\)/g, (match, label, subj, rest) => {
        return `[${label}](/api/download/${subj}/${rest})`;
    });

    // 3b. Rewrite markdown links pointing to /files/<subject>/.../<name>.zip into /api/download/<subject>/<name>
    out = out.replace(/(?<!!)\[([^\]]+)\]\(\/files\/([a-zA-Z0-9_-]+)\/(?:[^)]*\/)?([a-zA-Z0-9_-]+)\.zip\)/gi, (match, label, subj, pkg) => {
        return `[${label}](/api/download/${subj}/${pkg})`;
    });

    // 4. Strip hardcoded http(s)://<host>/ prefix on /api/download/... links inside markdown [label](...)
    out = out.replace(/(?<!!)\[([^\]]+)\]\(https?:\/\/[^/)]+(\/api\/download\/[^)]+)\)/gi, (match, label, dlPath) => {
        return `[${label}](${dlPath})`;
    });

    // 5. Convert bare http(s)://.../api/download/<subject>/<rest> or bare /api/download/<subject>/<rest> into clickable download links
    out = out.replace(/(?<![(\["'])(?:https?:\/\/[^\s/)]+)?(\/api\/download\/([a-zA-Z0-9_-]+)\/([a-zA-Z0-9_./-]+))/g, (match, dlPath, subj, rest) => {
        const cleanRest = rest.replace(/[.,;:]+$/, '');
        const cleanPath = `/api/download/${subj}/${cleanRest}`;
        const pkgName = cleanRest.split('/').pop().replace(/\.zip$/i, '');
        return `[📦 Download ${pkgName}.zip](${cleanPath})`;
    });

    return out;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function renderMarkdown(text, baseUrl = '') {
    if (!text) return '';
    try {
        const normalized = normalizeMarkdownImages(text, baseUrl);
        return marked.parse(normalized);
    } catch (e) {
        console.error("Markdown parsing failed:", e);
        return escapeHtml(text);
    }
}

function openImageLightbox(src, altText = '') {
    let overlay = document.getElementById('imageLightboxOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'imageLightboxOverlay';
        overlay.className = 'image-lightbox-overlay';
        overlay.innerHTML = `
            <div class="image-lightbox-content">
                <button class="image-lightbox-close" title="Close (Esc)">✕</button>
                <img class="image-lightbox-img" src="" alt="" />
                <div class="image-lightbox-caption"></div>
            </div>
        `;
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay || e.target.classList.contains('image-lightbox-close')) {
                overlay.style.display = 'none';
            }
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && overlay.style.display === 'flex') {
                overlay.style.display = 'none';
            }
        });
        document.body.appendChild(overlay);
    }
    const img = overlay.querySelector('.image-lightbox-img');
    const cap = overlay.querySelector('.image-lightbox-caption');
    img.src = src;
    img.alt = altText || '';
    cap.textContent = altText || '';
    overlay.style.display = 'flex';
}

