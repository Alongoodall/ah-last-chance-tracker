/**
 * AH Last Chance Tracker — Dashboard Application Logic
 *
 * Architecture:
 *   state   → single source of truth
 *   API     → thin wrappers around fetch()
 *   render  → pure DOM functions driven by state
 *   events  → wires UI interactions to state + render
 */

'use strict';

// ═══════════════════════════════════ STATE ═══════════════════════════════════

const state = {
  stores:          [],
  selectedStoreId: null,
  bargains:        [],         // raw from API (latest snapshot)
  filtered:        [],         // after client-side filters
  search:          '',
  minDiscount:     0,
  category:        '',         // '' = all
  inStockOnly:     true,
  favoritesOnly:   false,
  lastFetchedAt:   null,
  loading:         false,
  historyChart:    null,
  autoRefreshMs:   5 * 60 * 1000,   // 5 minutes
  autoRefreshTimer: null,
};

// ═══════════════════════════════════ FAVOURITES ══════════════════════════════

const FAV_KEY = 'ah_tracker_favourites';

const favourites = {
  _ids: new Set(JSON.parse(localStorage.getItem(FAV_KEY) || '[]')),

  has(productId) { return this._ids.has(productId); },

  toggle(productId) {
    if (this._ids.has(productId)) { this._ids.delete(productId); }
    else { this._ids.add(productId); }
    localStorage.setItem(FAV_KEY, JSON.stringify([...this._ids]));
  },

  count() { return this._ids.size; },
};

// ═══════════════════════════════════ API ═════════════════════════════════════

const API = {
  async getStores() {
    const r = await fetch('/api/stores');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },

  async getBargains(storeId, opts = {}) {
    const p = new URLSearchParams({ in_stock: opts.inStock ?? true });
    const r = await fetch(`/api/stores/${storeId}/bargains?${p}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },

  async getHistory(productId, storeId) {
    const r = await fetch(`/api/products/${productId}/history?store_id=${storeId}&limit=200`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  },
};

// ═══════════════════════════════════ HELPERS ═════════════════════════════════

function formatEuro(value) {
  if (value == null) return '–';
  return new Intl.NumberFormat('nl-NL', { style: 'currency', currency: 'EUR' }).format(value);
}

function formatDiscount(pct) {
  if (pct == null) return '–';
  return `−${Math.round(pct)}%`;
}

function discountTier(pct) {
  if (pct >= 70) return '70';
  if (pct >= 40) return '40';
  if (pct >= 25) return '25';
  return 'other';
}

function stockClass(n) {
  if (n <= 2)  return 'stock-low';
  if (n <= 5)  return 'stock-mid';
  return 'stock-ok';
}

function stockLabel(n) {
  if (n === 0) return '✕ uitverkocht';
  if (n === 1) return '1 over';
  return `${n}×`;
}

function timeAgo(date) {
  const secs = Math.floor((Date.now() - date) / 1000);
  if (secs < 60)  return `${secs}s geleden`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m geleden`;
  return `${Math.floor(secs / 3600)}u geleden`;
}

function formatDateTime(isoStr) {
  const d = new Date(isoStr);
  return d.toLocaleTimeString('nl-NL', { hour: '2-digit', minute: '2-digit' });
}

function formatDateTimeFull(isoStr) {
  const d = new Date(isoStr);
  return d.toLocaleString('nl-NL', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

// ═══════════════════════════════════ FILTER ══════════════════════════════════

function applyFilters() {
  const q = state.search.toLowerCase();
  state.filtered = state.bargains.filter(item => {
    const p = item.product;
    if (!p) return false;

    // Favourites-only mode
    if (state.favoritesOnly && !favourites.has(item.product_id)) return false;

    // Text search
    if (q) {
      const haystack = `${p.title} ${p.brand} ${p.category} ${p.sales_unit_size}`.toLowerCase();
      if (!haystack.includes(q)) return false;
    }

    // Min discount
    if (state.minDiscount > 0) {
      if ((item.markdown_percentage ?? 0) < state.minDiscount) return false;
    }

    // Category
    if (state.category && !(p.category || '').toLowerCase().includes(state.category.toLowerCase())) {
      return false;
    }

    // Stock
    if (state.inStockOnly && item.stock === 0) return false;

    return true;
  });
}

// ═══════════════════════════════════ RENDER ══════════════════════════════════

function renderStoreSelect() {
  const sel = document.getElementById('store-select');
  sel.innerHTML = state.stores.length === 0
    ? '<option value="">Geen winkels gevonden</option>'
    : state.stores.map(s => {
        // Show real name+city when available, fall back to store ID
        const hasName = s.name && s.name.trim();
        const label = hasName
          ? `${s.name}${s.city ? ' — ' + s.city : ''}`
          : `Winkel #${s.id}`;
        const snaps = s.snapshot_count > 0 ? ` (${s.snapshot_count}×)` : '';
        return `<option value="${s.id}" ${s.id === state.selectedStoreId ? 'selected' : ''}>
          ${label}${snaps}
        </option>`;
      }).join('');
}

function renderCategorySelect() {
  const categories = [...new Set(
    state.bargains.map(b => b.product?.category).filter(Boolean)
  )].sort();

  const sel = document.getElementById('category-select');
  sel.innerHTML = '<option value="">Alle categorieën</option>' +
    categories.map(c => `<option value="${c}" ${c === state.category ? 'selected' : ''}>${c}</option>`).join('');
}

function renderStats() {
  const items = state.filtered;
  const count70 = items.filter(i => (i.markdown_percentage ?? 0) >= 70).length;
  const count40 = items.filter(i => (i.markdown_percentage ?? 0) >= 40 && (i.markdown_percentage ?? 0) < 70).length;
  const count25 = items.filter(i => (i.markdown_percentage ?? 0) >= 25 && (i.markdown_percentage ?? 0) < 40).length;

  document.getElementById('stats-count').textContent =
    `${items.length} deal${items.length !== 1 ? 's' : ''} gevonden`;

  document.getElementById('stats-pills').innerHTML = [
    count70 > 0 ? `<span class="stat-pill stat-pill-70">70%: ${count70}</span>` : '',
    count40 > 0 ? `<span class="stat-pill stat-pill-40">40%: ${count40}</span>` : '',
    count25 > 0 ? `<span class="stat-pill stat-pill-25">25%: ${count25}</span>` : '',
  ].join('');
}

function renderRefresh() {
  const dot   = document.getElementById('refresh-dot');
  const label = document.getElementById('refresh-label');
  if (!state.lastFetchedAt) { dot.className = 'refresh-dot'; label.textContent = '–'; return; }

  const ageMs = Date.now() - state.lastFetchedAt;
  if (ageMs < 60_000) {
    dot.className = 'refresh-dot active';
    label.textContent = 'Zojuist bijgewerkt';
  } else if (ageMs < state.autoRefreshMs * 1.2) {
    dot.className = 'refresh-dot active';
    label.textContent = timeAgo(state.lastFetchedAt);
  } else {
    dot.className = 'refresh-dot stale';
    label.textContent = timeAgo(state.lastFetchedAt);
  }
}

function buildCard(item) {
  const p = item.product;
  if (!p) return '';

  const disc   = item.markdown_percentage ?? 0;
  const tier   = discountTier(disc);
  const saving = (item.price_was && item.price_now)
    ? item.price_was - item.price_now : null;
  const isFav = favourites.has(item.product_id);

  // Expiry days remaining
  let expiryStr = '';
  if (item.markdown_expiration_date) {
    const exp = new Date(item.markdown_expiration_date);
    const today = new Date(); today.setHours(0,0,0,0);
    const days = Math.round((exp - today) / 86_400_000);
    if (days === 0) expiryStr = '⏰ Verloopt vandaag';
    else if (days === 1) expiryStr = '📅 Verloopt morgen';
    else if (days > 0) expiryStr = `📅 THT: ${days}d`;
  }

  // Product image — graceful fallback to a coloured tile
  const imgHtml = p.image_url
    ? `<img
        class="card-img"
        src="${p.image_url}"
        alt="${p.title.replace(/"/g, '&quot;')}"
        loading="lazy"
        onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"
      />
      <div class="card-img-fallback" style="display:none"></div>`
    : `<div class="card-img-fallback"></div>`;

  const animDelay = `animation-delay: ${(Math.random() * 0.15).toFixed(2)}s`;

  return `
  <article
    class="card card-disc-${tier}"
    role="listitem"
    tabindex="0"
    data-product-id="${item.product_id}"
    data-product-title="${p.title.replace(/"/g, '&quot;')}"
    style="${animDelay}"
    aria-label="${p.title} – ${formatDiscount(disc)} korting"
  >
    <div class="card-img-wrap">
      ${imgHtml}
      <span class="card-badge badge-${tier}">${formatDiscount(disc)}</span>
      <button
        class="card-fav ${isFav ? 'active' : ''}"
        data-fav-id="${item.product_id}"
        aria-label="${isFav ? 'Verwijder uit favorieten' : 'Voeg toe aan favorieten'}"
        title="${isFav ? 'Favoriet verwijderen' : 'Als favoriet markeren'}"
      >★</button>
    </div>

    <div class="card-body">
      <div class="card-category">${p.category || '–'}</div>
      <h3 class="card-title">${p.title}</h3>
      <div class="card-brand">${p.brand || '–'}</div>

      <div class="card-prices">
        <span class="price-now">${formatEuro(item.price_now)}</span>
        ${item.price_was ? `<span class="price-was">${formatEuro(item.price_was)}</span>` : ''}
        ${saving ? `<span class="price-saving">−${formatEuro(saving)}</span>` : ''}
      </div>

      <div class="card-footer">
        <span class="card-unit">${p.sales_unit_size || ''}</span>
        <span class="stock-badge ${stockClass(item.stock)}">
          ${stockLabel(item.stock)}
        </span>
      </div>

      ${expiryStr ? `<div class="expiry-tag">${expiryStr}</div>` : ''}
    </div>
  </article>`;
}

function renderCards() {
  const grid = document.getElementById('card-grid');
  const msg  = document.getElementById('state-message');

  if (state.loading) {
    msg.innerHTML = '<div class="spinner"></div><p>Deals laden…</p>';
    msg.classList.remove('hidden');
    grid.innerHTML = '';
    return;
  }

  if (state.stores.length === 0) {
    msg.innerHTML = `
      <div class="empty-icon">🏪</div>
      <p>Geen winkels gevonden.</p>
      <p style="font-size:12px;margin-top:8px">
        Voer eerst een collectie uit:<br>
        <code style="background:var(--card);padding:4px 8px;border-radius:4px;font-size:11px">
          python scripts/collect_once.py --store-id 2203
        </code>
      </p>`;
    msg.classList.remove('hidden');
    grid.innerHTML = '';
    return;
  }

  if (state.filtered.length === 0) {
    const hasFilters = state.search || state.minDiscount > 0 || state.category;
    msg.innerHTML = hasFilters
      ? `<div class="empty-icon">🔍</div><p>Geen deals gevonden voor deze filters.</p>`
      : `<div class="empty-icon">🎉</div><p>Momenteel geen Laatste Kans deals voor deze winkel.</p>`;
    msg.classList.remove('hidden');
    grid.innerHTML = '';
    return;
  }

  msg.classList.add('hidden');
  grid.innerHTML = state.filtered.map(buildCard).join('');
}

// ═══════════════════════════════════ HISTORY PANEL ═══════════════════════════

function openHistoryPanel(productId, productTitle) {
  const panel = document.getElementById('history-panel');
  const overlay = document.getElementById('overlay');

  document.getElementById('history-title').textContent = productTitle;
  document.getElementById('history-subtitle').textContent = 'Laden…';
  document.getElementById('history-current').innerHTML = '';
  document.getElementById('history-table-body').innerHTML = '';

  if (state.historyChart) { state.historyChart.destroy(); state.historyChart = null; }
  const canvas = document.getElementById('history-chart');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');
  overlay.classList.add('show');
  document.body.classList.add('panel-open');

  API.getHistory(productId, state.selectedStoreId)
    .then(data => renderHistoryPanel(data))
    .catch(err  => {
      document.getElementById('history-subtitle').textContent = `Fout: ${err.message}`;
    });
}

function closeHistoryPanel() {
  const panel = document.getElementById('history-panel');
  panel.classList.remove('open');
  panel.setAttribute('aria-hidden', 'true');
  document.getElementById('overlay').classList.remove('show');
  document.body.classList.remove('panel-open');
}

function renderHistoryPanel(data) {
  const { product, entries } = data;

  // Subtitle
  document.getElementById('history-subtitle').textContent =
    `${product.brand} · ${product.sales_unit_size} · ${entries.length} meetpunt${entries.length !== 1 ? 'en' : ''}`;

  // Current stats (latest entry)
  const latest = entries[entries.length - 1];
  if (latest) {
    const discColor = (latest.markdown_percentage ?? 0) >= 70 ? 'red'
      : (latest.markdown_percentage ?? 0) >= 40 ? 'orange' : 'green';
    document.getElementById('history-current').innerHTML = `
      <div class="history-stat">
        <div class="history-stat-label">Huidige prijs</div>
        <div class="history-stat-value">${formatEuro(latest.price_now)}</div>
      </div>
      <div class="history-stat">
        <div class="history-stat-label">Korting</div>
        <div class="history-stat-value ${discColor}">${formatDiscount(latest.markdown_percentage)}</div>
      </div>
      <div class="history-stat">
        <div class="history-stat-label">Voorraad</div>
        <div class="history-stat-value">${latest.stock}</div>
      </div>`;
  }

  // Chart
  const labels = entries.map(e => formatDateTime(e.fetched_at));
  const prices  = entries.map(e => e.price_now);
  const discounts = entries.map(e => e.markdown_percentage);

  const chartFont = { family: "'Inter', sans-serif", size: 11 };
  const gridColor = 'rgba(255,255,255,0.05)';
  const textColor = '#94a3b8';

  state.historyChart = new Chart(document.getElementById('history-chart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        {
          label: 'Prijs (€)',
          data: prices,
          borderColor: '#1a7bff',
          backgroundColor: 'rgba(26,123,255,0.08)',
          borderWidth: 2,
          pointRadius: entries.length < 20 ? 4 : 2,
          pointBackgroundColor: '#1a7bff',
          tension: 0.3,
          fill: true,
          yAxisID: 'y',
        },
        {
          label: 'Korting (%)',
          data: discounts,
          borderColor: '#f97316',
          backgroundColor: 'rgba(249,115,22,0.06)',
          borderWidth: 2,
          pointRadius: entries.length < 20 ? 4 : 2,
          pointBackgroundColor: '#f97316',
          tension: 0.3,
          fill: true,
          yAxisID: 'y2',
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          labels: { color: textColor, font: chartFont, boxWidth: 12, padding: 12 },
        },
        tooltip: {
          backgroundColor: 'rgba(20,31,51,0.95)',
          borderColor: 'rgba(255,255,255,0.1)',
          borderWidth: 1,
          titleColor: '#f1f5f9',
          bodyColor: '#94a3b8',
          callbacks: {
            label(ctx) {
              return ctx.datasetIndex === 0
                ? ` ${formatEuro(ctx.raw)}`
                : ` ${ctx.raw != null ? Math.round(ctx.raw) + '%' : '–'}`;
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color: textColor, font: chartFont, maxRotation: 0, maxTicksLimit: 6 },
          grid: { color: gridColor },
        },
        y: {
          position: 'left',
          ticks: { color: '#1a7bff', font: chartFont, callback: v => formatEuro(v) },
          grid: { color: gridColor },
        },
        y2: {
          position: 'right',
          ticks: { color: '#f97316', font: chartFont, callback: v => `${v}%` },
          grid: { drawOnChartArea: false },
          min: 0, max: 100,
        },
      },
    },
  });

  // Table
  const tbody = document.getElementById('history-table-body');
  tbody.innerHTML = [...entries].reverse().map(e => {
    const disc = e.markdown_percentage != null ? Math.round(e.markdown_percentage) : null;
    const discColor = disc >= 70 ? 'var(--disc-70)' : disc >= 40 ? 'var(--disc-40)' : 'var(--disc-25)';
    return `
    <tr>
      <td>${formatDateTimeFull(e.fetched_at)}</td>
      <td class="mono" style="font-weight:600">${formatEuro(e.price_now)}</td>
      <td class="mono" style="color:${discColor};font-weight:600">${disc != null ? disc + '%' : '–'}</td>
      <td class="mono">${e.stock}</td>
    </tr>`;
  }).join('');
}

// ═══════════════════════════════════ DATA LOADING ════════════════════════════

async function loadStores() {
  try {
    state.stores = await API.getStores();
    if (state.stores.length > 0 && !state.selectedStoreId) {
      state.selectedStoreId = state.stores[0].id;
    }
    renderStoreSelect();
  } catch (err) {
    console.error('loadStores:', err);
  }
}

async function loadBargains() {
  if (!state.selectedStoreId) {
    state.loading = false;
    renderCards();
    return;
  }

  state.loading = true;
  renderCards();

  const btn = document.getElementById('btn-refresh');
  btn.classList.add('spinning');

  try {
    // Always fetch all stock levels — filter client-side
    state.bargains = await API.getBargains(state.selectedStoreId, { inStock: false });
    state.lastFetchedAt = Date.now();
    renderCategorySelect();
  } catch (err) {
    console.error('loadBargains:', err);
    state.bargains = [];
  } finally {
    state.loading = false;
    btn.classList.remove('spinning');
    applyFilters();
    renderCards();
    renderStats();
    renderRefresh();
  }
}

// ═══════════════════════════════════ AUTO REFRESH ════════════════════════════

function scheduleAutoRefresh() {
  if (state.autoRefreshTimer) clearTimeout(state.autoRefreshTimer);
  state.autoRefreshTimer = setTimeout(async () => {
    await loadBargains();
    scheduleAutoRefresh();
  }, state.autoRefreshMs);
}

// Update the "X min ago" label every 30 seconds
setInterval(renderRefresh, 30_000);

// Update the favourites chip label and active state
function _updateFavChip() {
  const btn = document.getElementById('chip-fav');
  if (!btn) return;
  const count = favourites.count();
  btn.textContent = count > 0 ? `★ Favorieten (${count})` : '★ Favorieten';
  btn.setAttribute('aria-pressed', state.favoritesOnly ? 'true' : 'false');
}

// ═══════════════════════════════════ EVENTS ══════════════════════════════════

function wireEvents() {
  // Store selector
  document.getElementById('store-select').addEventListener('change', e => {
    state.selectedStoreId = parseInt(e.target.value, 10) || null;
    loadBargains().then(scheduleAutoRefresh);
  });

  // Manual refresh
  document.getElementById('btn-refresh').addEventListener('click', () => {
    loadBargains().then(scheduleAutoRefresh);
  });

  // Search
  let searchTimer;
  document.getElementById('search-input').addEventListener('input', e => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.search = e.target.value.trim();
      applyFilters();
      renderCards();
      renderStats();
    }, 150);
  });

  // Discount chips (and favourites chip)
  document.querySelectorAll('.chip[data-discount]').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      state.minDiscount = parseInt(chip.dataset.discount, 10) || 0;
      state.favoritesOnly = false;
      _updateFavChip();
      applyFilters();
      renderCards();
      renderStats();
    });
  });

  // Favourites chip — independent of discount chips
  const favChip = document.getElementById('chip-fav');
  if (favChip) {
    favChip.addEventListener('click', () => {
      state.favoritesOnly = !state.favoritesOnly;
      favChip.classList.toggle('active', state.favoritesOnly);
      // Deactivate discount chips when entering fav-only mode
      if (state.favoritesOnly) {
        document.querySelectorAll('.chip[data-discount]').forEach(c => c.classList.remove('active'));
      } else {
        document.getElementById('chip-all').classList.add('active');
      }
      _updateFavChip();
      applyFilters();
      renderCards();
      renderStats();
    });
    _updateFavChip(); // Set initial count
  }

  // Category
  document.getElementById('category-select').addEventListener('change', e => {
    state.category = e.target.value;
    applyFilters();
    renderCards();
    renderStats();
  });

  // Stock toggle
  document.getElementById('in-stock-toggle').addEventListener('change', e => {
    state.inStockOnly = e.target.checked;
    applyFilters();
    renderCards();
    renderStats();
  });

  // Card clicks (event delegation)
  document.getElementById('card-grid').addEventListener('click', e => {
    // Favourite star button — toggle without opening history panel
    const favBtn = e.target.closest('.card-fav');
    if (favBtn) {
      e.stopPropagation();
      const productId = parseInt(favBtn.dataset.favId, 10);
      favourites.toggle(productId);
      // Update the button in place without re-rendering everything
      const isNowFav = favourites.has(productId);
      favBtn.classList.toggle('active', isNowFav);
      favBtn.setAttribute('aria-label', isNowFav ? 'Verwijder uit favorieten' : 'Voeg toe aan favorieten');
      favBtn.title = isNowFav ? 'Favoriet verwijderen' : 'Als favoriet markeren';
      // If in favourites-only mode, remove the card immediately
      if (state.favoritesOnly && !isNowFav) {
        favBtn.closest('.card').remove();
        state.filtered = state.filtered.filter(i => i.product_id !== productId);
        renderStats();
      }
      // Update favourites chip count
      _updateFavChip();
      return;
    }

    const card = e.target.closest('.card');
    if (!card) return;
    openHistoryPanel(
      parseInt(card.dataset.productId, 10),
      card.dataset.productTitle,
    );
  });

  document.getElementById('card-grid').addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      const card = e.target.closest('.card');
      if (card) { e.preventDefault(); card.click(); }
    }
  });

  // Close history panel
  document.getElementById('btn-close-history').addEventListener('click', closeHistoryPanel);
  document.getElementById('overlay').addEventListener('click', closeHistoryPanel);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeHistoryPanel();
  });
}

// ═══════════════════════════════════ INIT ════════════════════════════════════

async function init() {
  wireEvents();
  await loadStores();
  await loadBargains();
  scheduleAutoRefresh();
}

document.addEventListener('DOMContentLoaded', init);
