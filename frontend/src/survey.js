import { sections, Q_TYPE, SURVEY_META } from './questions.js';

const STORAGE_KEY = 'auri_survey_responses';
const STORAGE_PAGE_KEY = 'auri_survey_page';
const API_BASE = import.meta.env.VITE_API_BASE || (
  location.hostname === 'localhost' ? '' : 'https://alris.ddns.net:8443/lg'
);

const STATUS_META = {
  open:      { label: '열림',     icon: '📌', color: '#6b7280', bg: '#f3f4f6' },
  in_review: { label: '검토중',   icon: '🟡', color: '#b45309', bg: '#fef3c7' },
  resolved:  { label: '반영완료', icon: '🟢', color: '#15803d', bg: '#dcfce7' },
  rejected:  { label: '보류',     icon: '⚪', color: '#4b5563', bg: '#e5e7eb' },
};

const GATE = {
  LOADING: 'loading',
  DENIED: 'denied',
  RESUBMIT_CHOICE: 'resubmit_choice',
  READ_ONLY: 'read_only',
  OPEN: 'open',
  REGISTER: 'register',  // 토큰 없는 공개 진입 — 랜딩/동의/정보입력 단계 진행
  REVIEW: 'review',      // ?review=1 진입 — 본인 응답 읽기전용 확인
};

const REG_STEP = {
  LANDING: 'landing',
  CONSENT: 'consent',
  INFO: 'info',
};

const REGISTER_DRAFT_KEY = 'auri_register_draft';

const EDIT_MODE = {
  NEW: 'new',
  EDIT: 'edit',
};

export class SurveyEngine {
  constructor(container) {
    this.container = container;
    const urlParams = new URLSearchParams(window.location.search);
    this.token = urlParams.get('token');
    this.reviewMode = urlParams.get('review') === '1';
    this.justRegistered = urlParams.get('just_registered') === '1';
    this.participant = null;
    this.submitted = false;
    this.submittedAt = null;
    this.updatedAt = null;
    this.editMode = EDIT_MODE.NEW;
    this.gate = this.token ? GATE.LOADING : GATE.REGISTER;
    this.regStep = REG_STEP.LANDING;
    this.regDraft = this.loadRegisterDraft();
    this.regError = '';
    this.regSubmitting = false;
    this.responses = this.loadResponses();
    this.threads = {};                // { qid: [comment, ...] } — fetched from server
    this.threadsLoading = false;
    this.threadDrafts = {};           // { qid: "draft text" } — top-level new comment
    this.threadReplyDrafts = {};      // { parent_id: "draft text" } — reply form
    this.threadOpenReply = null;      // 현재 답글 입력창이 열려 있는 parent_id
    this.threadOpenEdit = null;       // 현재 편집 중인 comment id
    this.threadEditDrafts = {};       // { comment_id: "edited text" }
    this.threadError = '';            // 마지막 스레드 오류 메시지
    this.currentPage = 0;
    this.visibleSections = [];
    this.editingParticipant = false;
    this.participantFormError = '';

    if (this.token) {
      this.verifyToken().then(async () => {
        if (this.isReviewer()) {
          await this.fetchThreads();
        }
        this.render();
      });
    } else {
      this.render();
    }
  }

  async verifyToken() {
    try {
      const res = await fetch(`${API_BASE}/api/survey/${this.token}`);
      if (!res.ok) {
        this.gate = GATE.DENIED;
        return;
      }
      const data = await res.json();
      this.participant = data;
      this.submittedAt = data.submitted_at || null;
      this.updatedAt = data.updated_at || null;
      if (data.has_responded && data.responses) {
        this.responses = { ...this.responses, ...data.responses };
        this.saveResponses();
        this.submitted = true;
        // ?review=1 URL 파라미터로 들어왔고 응답이 있으면 곧바로 읽기전용 화면
        this.gate = this.reviewMode ? GATE.REVIEW : GATE.RESUBMIT_CHOICE;
      } else {
        // 아직 응답 없으면 review 요청도 일반 설문 화면으로 (안내차)
        this.gate = GATE.OPEN;
      }
      // Q1(지자체 유형) 자동 충족 — category가 광역/기초인 경우 응답 미리 채움
      if (this.responses['Q1'] === undefined) {
        if (data.category === '광역자치단체') this.responses['Q1'] = 0;
        else if (data.category === '기초자치단체') this.responses['Q1'] = 1;
        if (this.responses['Q1'] !== undefined) this.saveResponses();
      }
    } catch {
      this.gate = GATE.DENIED;
    }
  }

  // ── Persistence ──
  loadResponses() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      return saved ? JSON.parse(saved) : {};
    } catch { return {}; }
  }

  saveResponses() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(this.responses));
    localStorage.setItem(STORAGE_PAGE_KEY, String(this.currentPage));
  }

  isReviewer() {
    return this.participant && this.participant.category === '연구진';
  }

  // ── Review Comment Threads ──
  async fetchThreads() {
    if (!this.isReviewer() || !this.token) return;
    this.threadsLoading = true;
    try {
      const res = await fetch(`${API_BASE}/api/survey/${this.token}/threads`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      this.threads = data.threads || {};
      this.threadError = '';
    } catch (e) {
      console.warn('threads fetch failed', e);
      this.threadError = '코멘트 스레드를 불러오지 못했습니다.';
    } finally {
      this.threadsLoading = false;
    }
  }

  /** qid에 달린 모든 코멘트(parent + replies)를 부모-자식 트리로 재구성 */
  buildThreadTree(qid) {
    const all = this.threads[qid] || [];
    const byId = new Map(all.map(c => [c.id, { ...c, children: [] }]));
    const roots = [];
    for (const node of byId.values()) {
      if (node.parent_id && byId.has(node.parent_id)) {
        byId.get(node.parent_id).children.push(node);
      } else {
        roots.push(node);
      }
    }
    return roots;
  }

  renderReviewerThread(qid) {
    const roots = this.buildThreadTree(qid);
    const totalCount = (this.threads[qid] || []).length;

    let body = '';
    if (totalCount === 0) {
      body = '<p class="thread-empty">아직 등록된 코멘트가 없습니다. 첫 코멘트를 남겨보세요.</p>';
    } else {
      body = roots.map(r => this.renderCommentNode(qid, r, 0)).join('');
    }

    const draft = this.threadDrafts[qid] || '';
    const newForm = `
      <div class="thread-new-form">
        <textarea class="thread-textarea" data-thread-new="${qid}" rows="2"
          placeholder="이 문항에 대한 검토 의견·수정 요청·논의 사항을 작성하세요.">${this.escape(draft)}</textarea>
        <div class="thread-form-actions">
          <button class="btn btn-thread" data-thread-submit="${qid}">코멘트 등록</button>
        </div>
      </div>
    `;

    return `
      <div class="reviewer-thread" data-qid="${qid}">
        <div class="thread-header">
          <span class="thread-title">💬 검토 코멘트</span>
          <span class="thread-count">${totalCount}건</span>
          <span class="thread-sub">(연구진·관리자 모두에게 공개)</span>
          <button class="thread-refresh-btn" data-thread-refresh="${qid}" title="새로고침">↻</button>
        </div>
        <div class="thread-body">${body}</div>
        ${newForm}
      </div>
    `;
  }

  async refetchAndRefreshThread(qid) {
    await this.fetchThreads();
    this.refreshThread(qid);
  }

  renderCommentNode(qid, c, depth) {
    const status = STATUS_META[c.status] || STATUS_META.open;
    const isMine = c.author_token === this.token;
    const roleBadge = c.author_role === 'admin'
      ? '<span class="role-badge role-admin">관리자</span>'
      : '<span class="role-badge role-reviewer">연구진</span>';
    const orgPart = c.author_org ? ` · ${this.escape(c.author_org)}` : '';
    const editedMark = c.updated_at ? ' <span class="edited-mark">(수정됨)</span>' : '';

    const isEditing = this.threadOpenEdit === c.id;
    const editDraft = this.threadEditDrafts[c.id] ?? c.text;

    const textHtml = isEditing
      ? `
        <div class="comment-edit-form">
          <textarea class="thread-textarea" data-thread-edit="${c.id}" rows="2">${this.escape(editDraft)}</textarea>
          <div class="thread-form-actions">
            <button class="btn btn-thread btn-sm" data-thread-edit-save="${c.id}" data-qid="${qid}">저장</button>
            <button class="btn btn-thread-cancel btn-sm" data-thread-edit-cancel="${c.id}">취소</button>
          </div>
        </div>
      `
      : `<div class="comment-text">${this.escape(c.text).replace(/\n/g, '<br>')}${editedMark}</div>`;

    const actions = [];
    actions.push(`<button class="btn-link-sm" data-thread-reply="${c.id}" data-qid="${qid}">↳ 답글</button>`);
    if (isMine && !isEditing) {
      actions.push(`<button class="btn-link-sm" data-thread-edit-open="${c.id}">편집</button>`);
      actions.push(`<button class="btn-link-sm danger" data-thread-delete="${c.id}" data-qid="${qid}">삭제</button>`);
    }

    const replyOpen = this.threadOpenReply === c.id;
    const replyDraft = this.threadReplyDrafts[c.id] || '';
    const replyForm = replyOpen
      ? `
        <div class="comment-reply-form">
          <textarea class="thread-textarea" data-thread-reply-input="${c.id}" rows="2"
            placeholder="답글 작성…">${this.escape(replyDraft)}</textarea>
          <div class="thread-form-actions">
            <button class="btn btn-thread btn-sm" data-thread-reply-submit="${c.id}" data-qid="${qid}">답글 등록</button>
            <button class="btn btn-thread-cancel btn-sm" data-thread-reply-cancel="${c.id}">취소</button>
          </div>
        </div>
      `
      : '';

    const childrenHtml = (c.children || [])
      .map(cc => this.renderCommentNode(qid, cc, depth + 1))
      .join('');

    return `
      <div class="comment-node depth-${Math.min(depth, 3)}" data-cid="${c.id}">
        <div class="comment-card">
          <div class="comment-meta">
            <span class="comment-author">${this.escape(c.author_name || '익명')}</span>
            ${roleBadge}
            <span class="comment-org">${orgPart}</span>
            <span class="comment-time">· ${this.formatRelativeTime(c.created_at)}</span>
            <span class="comment-status" style="background:${status.bg};color:${status.color}">${status.icon} ${status.label}</span>
          </div>
          ${textHtml}
          <div class="comment-actions">${actions.join(' · ')}</div>
        </div>
        ${replyForm}
        ${childrenHtml ? `<div class="comment-children">${childrenHtml}</div>` : ''}
      </div>
    `;
  }

  // ── Thread API ──
  async submitNewComment(qid) {
    const text = (this.threadDrafts[qid] || '').trim();
    if (!text) {
      alert('코멘트 내용을 입력해 주세요.');
      return;
    }
    try {
      const res = await fetch(`${API_BASE}/api/survey/${this.token}/threads/${qid}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (!this.threads[qid]) this.threads[qid] = [];
      this.threads[qid].push(data.comment);
      this.threadDrafts[qid] = '';
      this.refreshThread(qid);
    } catch (e) {
      alert(`코멘트 등록 실패: ${e.message}`);
    }
  }

  async submitReply(parentId, qid) {
    const text = (this.threadReplyDrafts[parentId] || '').trim();
    if (!text) { alert('답글 내용을 입력해 주세요.'); return; }
    try {
      const res = await fetch(`${API_BASE}/api/survey/${this.token}/threads/${qid}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, parent_id: parentId }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (!this.threads[qid]) this.threads[qid] = [];
      this.threads[qid].push(data.comment);
      this.threadReplyDrafts[parentId] = '';
      this.threadOpenReply = null;
      this.refreshThread(qid);
    } catch (e) {
      alert(`답글 등록 실패: ${e.message}`);
    }
  }

  async submitEdit(commentId, qid) {
    const text = (this.threadEditDrafts[commentId] || '').trim();
    if (!text) { alert('내용을 입력해 주세요.'); return; }
    try {
      const res = await fetch(`${API_BASE}/api/survey/${this.token}/threads/${qid}/${commentId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      const list = this.threads[qid] || [];
      const idx = list.findIndex(c => c.id === commentId);
      if (idx >= 0) list[idx] = data.comment;
      this.threadOpenEdit = null;
      delete this.threadEditDrafts[commentId];
      this.refreshThread(qid);
    } catch (e) {
      alert(`수정 실패: ${e.message}`);
    }
  }

  async deleteComment(commentId, qid) {
    if (!confirm('이 코멘트를 삭제하시겠습니까?')) return;
    try {
      const res = await fetch(`${API_BASE}/api/survey/${this.token}/threads/${qid}/${commentId}`, {
        method: 'DELETE',
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      if (data.status === 'deleted') {
        this.threads[qid] = (this.threads[qid] || []).filter(c => c.id !== commentId);
      } else {
        // soft_deleted: 본문만 비우고 entry 유지
        const list = this.threads[qid] || [];
        const idx = list.findIndex(c => c.id === commentId);
        if (idx >= 0) list[idx] = { ...list[idx], text: '(작성자가 삭제한 코멘트)' };
      }
      this.refreshThread(qid);
    } catch (e) {
      alert(`삭제 실패: ${e.message}`);
    }
  }

  /** 특정 qid의 thread 영역만 다시 그린다 (전체 페이지 재렌더 회피) */
  refreshThread(qid) {
    const wrap = this.container.querySelector(`.reviewer-thread[data-qid="${qid}"]`);
    if (!wrap) return;
    wrap.outerHTML = this.renderReviewerThread(qid);
    // outerHTML 교체 후 같은 qid의 새 wrap을 다시 찾아 그 안에서만 바인딩 (중복 방지)
    const fresh = this.container.querySelector(`.reviewer-thread[data-qid="${qid}"]`);
    if (fresh) this.bindThreadEvents(fresh);
  }

  bindThreadEvents(scope) {
    if (!this.isReviewer()) return;
    const root = scope || this.container;

    root.querySelectorAll('[data-thread-new]').forEach(el => {
      const qid = el.dataset.threadNew;
      el.addEventListener('input', () => { this.threadDrafts[qid] = el.value; });
    });
    root.querySelectorAll('[data-thread-submit]').forEach(btn => {
      btn.addEventListener('click', () => this.submitNewComment(btn.dataset.threadSubmit));
    });

    root.querySelectorAll('[data-thread-reply]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.threadOpenReply = btn.dataset.threadReply;
        this.refreshThread(btn.dataset.qid);
      });
    });
    root.querySelectorAll('[data-thread-reply-input]').forEach(el => {
      const pid = el.dataset.threadReplyInput;
      el.addEventListener('input', () => { this.threadReplyDrafts[pid] = el.value; });
    });
    root.querySelectorAll('[data-thread-reply-submit]').forEach(btn => {
      btn.addEventListener('click', () => this.submitReply(btn.dataset.threadReplySubmit, btn.dataset.qid));
    });
    root.querySelectorAll('[data-thread-reply-cancel]').forEach(btn => {
      btn.addEventListener('click', () => {
        const pid = btn.dataset.threadReplyCancel;
        this.threadOpenReply = null;
        delete this.threadReplyDrafts[pid];
        const card = btn.closest('.comment-node');
        const qid = card?.closest('.reviewer-thread')?.dataset.qid;
        if (qid) this.refreshThread(qid);
      });
    });

    root.querySelectorAll('[data-thread-edit-open]').forEach(btn => {
      btn.addEventListener('click', () => {
        const cid = btn.dataset.threadEditOpen;
        this.threadOpenEdit = cid;
        const card = btn.closest('.comment-node');
        const qid = card?.closest('.reviewer-thread')?.dataset.qid;
        const list = this.threads[qid] || [];
        const c = list.find(x => x.id === cid);
        if (c) this.threadEditDrafts[cid] = c.text;
        if (qid) this.refreshThread(qid);
      });
    });
    root.querySelectorAll('[data-thread-edit]').forEach(el => {
      const cid = el.dataset.threadEdit;
      el.addEventListener('input', () => { this.threadEditDrafts[cid] = el.value; });
    });
    root.querySelectorAll('[data-thread-edit-save]').forEach(btn => {
      btn.addEventListener('click', () => this.submitEdit(btn.dataset.threadEditSave, btn.dataset.qid));
    });
    root.querySelectorAll('[data-thread-edit-cancel]').forEach(btn => {
      btn.addEventListener('click', () => {
        const cid = btn.dataset.threadEditCancel;
        this.threadOpenEdit = null;
        delete this.threadEditDrafts[cid];
        const card = btn.closest('.comment-node');
        const qid = card?.closest('.reviewer-thread')?.dataset.qid;
        if (qid) this.refreshThread(qid);
      });
    });
    root.querySelectorAll('[data-thread-delete]').forEach(btn => {
      btn.addEventListener('click', () => this.deleteComment(btn.dataset.threadDelete, btn.dataset.qid));
    });
    root.querySelectorAll('[data-thread-refresh]').forEach(btn => {
      btn.addEventListener('click', () => this.refetchAndRefreshThread(btn.dataset.threadRefresh));
    });
  }

  formatRelativeTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const diff = Date.now() - d.getTime();
    const min = Math.floor(diff / 60000);
    if (min < 1) return '방금';
    if (min < 60) return `${min}분 전`;
    const h = Math.floor(min / 60);
    if (h < 24) return `${h}시간 전`;
    const days = Math.floor(h / 24);
    if (days < 7) return `${days}일 전`;
    return this.formatDateTime(iso);
  }

  getResponse(id) { return this.responses[id]; }
  setResponse(id, value) {
    this.responses[id] = value;
    this.saveResponses();
  }

  // ── Section Visibility (branching) ──
  updateVisibleSections() {
    this.visibleSections = sections.filter(s => {
      if (!s.showWhen) return true;
      const sw = s.showWhen;
      const val = this.responses[sw.qid];
      if (val === undefined || val === null) return false;
      if (Array.isArray(sw.in)) {
        return sw.in.includes(val);
      }
      if (sw.includes !== undefined) {
        return Array.isArray(val) && val.includes(sw.includes);
      }
      if (sw.value !== undefined) {
        return val === sw.value;
      }
      return true;
    });
  }

  // ── Persistence: Register Draft ──
  loadRegisterDraft() {
    try {
      const saved = localStorage.getItem(REGISTER_DRAFT_KEY);
      return saved ? JSON.parse(saved) : this.emptyRegisterDraft();
    } catch { return this.emptyRegisterDraft(); }
  }

  emptyRegisterDraft() {
    return {
      email: '', org: '', category: '',
      dept: '', team: '', position: '', rank: '', duty: '',
      consent_pi: false, consent_reward: false,
      reward_name: '', reward_phone: '',
    };
  }

  saveRegisterDraft() {
    try {
      localStorage.setItem(REGISTER_DRAFT_KEY, JSON.stringify(this.regDraft));
    } catch {}
  }

  clearRegisterDraft() {
    try { localStorage.removeItem(REGISTER_DRAFT_KEY); } catch {}
  }

  // ── Render Router ──
  render() {
    if (this.gate === GATE.LOADING) {
      this.renderLoading();
      return;
    }
    if (this.gate === GATE.DENIED) {
      this.renderAccessDenied();
      return;
    }
    if (this.gate === GATE.REGISTER) {
      this.renderRegister();
      return;
    }
    if (this.gate === GATE.RESUBMIT_CHOICE) {
      this.renderResubmitChoice();
      return;
    }
    if (this.gate === GATE.REVIEW || this.gate === GATE.READ_ONLY) {
      this.renderReview();
      return;
    }

    this.updateVisibleSections();
    if (this.currentPage === 0) {
      this.renderIntro();
    } else if (this.currentPage > this.visibleSections.length) {
      this.renderCompletion();
    } else {
      this.renderSection(this.visibleSections[this.currentPage - 1]);
    }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // ── Loading ──
  renderLoading() {
    this.container.innerHTML = `
      <div class="survey-container">
        <div class="completion" style="padding:160px 20px">
          <div class="spinner"></div>
          <style>@keyframes spin{to{transform:rotate(360deg)}}.spinner{width:40px;height:40px;border:3px solid #e0e0e0;border-top:3px solid #2c2c2c;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 24px}</style>
          <p style="color:var(--c-text-secondary)">설문 링크를 확인 중입니다…</p>
        </div>
      </div>
    `;
  }

  // ── Register (공개 단일 링크 자가등록) ──
  renderRegister() {
    if (this.regStep === REG_STEP.LANDING) return this.renderRegisterLanding();
    if (this.regStep === REG_STEP.CONSENT) return this.renderRegisterConsent();
    if (this.regStep === REG_STEP.INFO) return this.renderRegisterInfo();
  }

  renderRegisterLanding() {
    const m = SURVEY_META;
    this.container.innerHTML = `
      <div class="survey-container">
        <div class="register-landing">
          <div class="register-institution">${m.institution}</div>
          <h1 class="register-title">${m.title}</h1>
          <div class="register-subtitle">${m.subtitle}</div>

          <div class="register-card">
            <h2>조사 목적</h2>
            <p>본 조사는 국가 공공자산인 지방자치단체 청사의 체계적 관리를 위한 법적 기반 마련과,
               중앙·지방 간 관리 격차 해소를 위한 정책 기초 자료 확보를 목적으로 합니다.
               「청사 관리에 관한 법률(가칭)」 제정을 위한 실태조사의 일환으로 수행됩니다.</p>
          </div>

          <div class="register-card">
            <h2>참여 안내</h2>
            <ul>
              <li>응답 대상: 전국 광역·기초 지방자치단체 <strong>청사 관리·공유재산 담당자</strong></li>
              <li>소요 시간: 약 ${m.duration}</li>
              <li>응답 중간에 자동 저장되며 링크를 다시 열면 이어서 작성할 수 있습니다.</li>
              <li>모든 응답은 「통계법」 제33조(비밀의 보호)에 따라 통계 목적으로만 사용됩니다.</li>
            </ul>
          </div>

          <div class="register-card register-reward">
            <h2>🎁 사례품 안내</h2>
            <p>설문에 끝까지 응답해 주신 분께는 감사의 표시로 <strong>모바일 쿠폰</strong>을 발송해 드립니다.
               (수령을 원하시는 경우 다음 단계에서 휴대폰 번호 활용에 동의해 주시기 바랍니다.)</p>
          </div>

          <div class="register-meta">
            <dl>
              <dt>조사기관</dt><dd>${m.institution}</dd>
              <dt>연구책임</dt><dd>${m.researcher}</dd>
              <dt>문의</dt><dd>${m.contact}</dd>
            </dl>
          </div>

          <button class="btn-start" id="btn-reg-start">참여하기 →</button>
        </div>
      </div>
    `;
    this.container.querySelector('#btn-reg-start')?.addEventListener('click', () => {
      this.regStep = REG_STEP.CONSENT;
      this.render();
    });
  }

  renderRegisterConsent() {
    const d = this.regDraft;
    const errHtml = this.regError ? `<p class="register-error">${this.escape(this.regError)}</p>` : '';

    this.container.innerHTML = `
      <div class="survey-container">
        <div class="register-stepper">
          <span class="step done">①</span><span class="step-line"></span>
          <span class="step active">②</span><span class="step-line"></span>
          <span class="step">③</span>
        </div>

        <div class="register-card">
          <h2>개인정보 수집·이용 동의</h2>
          <p class="register-hint">「개인정보 보호법」 제15조에 따라 아래 사항을 안내드립니다.</p>

          <div class="consent-info">
            <h3>ⓘ 본 설문은 익명으로 진행됩니다 — 통계 처리 안내 (동의 불요)</h3>
            <p>
              지자체 유형(광역/기초)·소속·부서·팀·직위·직급·담당업무 등 <strong>분류 정보</strong>와
              <strong>설문 응답 내용</strong>은 「<strong>통계법」 제33조(비밀의 보호)</strong>에 따라 통계 처리되어
              <strong>개인을 식별할 수 없는 형태</strong>로만 분석·공표됩니다.
              연구 결과의 보고서·논문 등에서 개별 응답자 또는 기관의 답변이 그대로 노출되지 않으며,
              위 분류 정보와 응답 내용은 별도의 동의 절차를 거치지 않습니다.
            </p>
          </div>

          <div class="consent-block">
            <h3>① 필수동의 — 응답 완료 안내 메일 발송 목적</h3>
            <table class="consent-table">
              <tbody>
                <tr><th>수집 항목</th>
                  <td><strong>이메일 주소</strong></td></tr>
                <tr><th>수집·이용 목적</th>
                  <td>설문 응답 완료 안내 메일 발송 (본인 응답 확인 링크 포함).
                      <strong>응답 분석·통계 처리 단계에서는 사용하지 않으며, 개인 식별 자료로 활용되지 않습니다.</strong></td></tr>
                <tr><th>보유·이용 기간</th>
                  <td>완료 메일 발송 후 즉시 파기 (응답 수정·재발송 요청 처리 시 연구 종료 시점까지 한정 보존)</td></tr>
                <tr><th>거부 권리</th>
                  <td>동의를 거부할 수 있으며, 거부 시 본 설문에 참여하실 수 없습니다.</td></tr>
              </tbody>
            </table>
            <label class="consent-check">
              <input type="checkbox" id="reg-consent-pi" ${d.consent_pi ? 'checked' : ''}>
              <span>위 사항을 충분히 이해하였으며, 응답 완료 안내 메일 발송 목적의 이메일 수집·이용에 <strong>동의합니다(필수).</strong></span>
            </label>
          </div>

          <div class="consent-block">
            <h3>② 선택동의 — 사례품(모바일 쿠폰) 발송 목적</h3>
            <table class="consent-table">
              <tbody>
                <tr><th>수집 항목</th>
                  <td>이름, 휴대폰 번호</td></tr>
                <tr><th>수집·이용 목적</th>
                  <td>설문 참여 사례품(모바일 쿠폰) 발송. <strong>분석·통계 처리에는 사용하지 않습니다.</strong></td></tr>
                <tr><th>보유·이용 기간</th>
                  <td>발송 완료 후 즉시 파기 (지급 분쟁 시 6개월까지 한정 보존)</td></tr>
                <tr><th>거부 권리</th>
                  <td>동의를 거부할 수 있으며, 거부 시 사례품 발송이 불가합니다. (설문 참여는 가능)</td></tr>
              </tbody>
            </table>
            <label class="consent-check">
              <input type="checkbox" id="reg-consent-reward" ${d.consent_reward ? 'checked' : ''}>
              <span>사례품 발송을 위한 이름·휴대폰 번호 수집·이용에 <strong>동의합니다(선택).</strong></span>
            </label>
          </div>

          ${errHtml}

          <div class="register-actions">
            <button class="btn btn-prev" id="btn-reg-back">← 이전</button>
            <button class="btn btn-next" id="btn-reg-next">다음 →</button>
          </div>
        </div>
      </div>
    `;
    this.bindConsentEvents();
  }

  bindConsentEvents() {
    this.container.querySelector('#btn-reg-back')?.addEventListener('click', () => {
      this.regStep = REG_STEP.LANDING;
      this.regError = '';
      this.render();
    });
    this.container.querySelector('#reg-consent-pi')?.addEventListener('change', (e) => {
      this.regDraft.consent_pi = e.target.checked;
      this.saveRegisterDraft();
    });
    this.container.querySelector('#reg-consent-reward')?.addEventListener('change', (e) => {
      this.regDraft.consent_reward = e.target.checked;
      this.saveRegisterDraft();
    });
    this.container.querySelector('#btn-reg-next')?.addEventListener('click', () => {
      if (!this.regDraft.consent_pi) {
        this.regError = '필수동의(①)에 체크해 주셔야 다음 단계로 진행할 수 있습니다.';
        this.render();
        return;
      }
      this.regError = '';
      this.regStep = REG_STEP.INFO;
      this.render();
    });
  }

  renderRegisterInfo() {
    const d = this.regDraft;
    const errHtml = this.regError ? `<p class="register-error">${this.escape(this.regError)}</p>` : '';
    const submittingHtml = this.regSubmitting
      ? '<span class="register-submitting">등록 중…</span>' : '';

    const rewardBlock = d.consent_reward ? `
      <div class="register-section reward-section">
        <h3>🎁 사례품 수령 정보 <span class="required">(선택동의 시 필수)</span></h3>
        <p class="register-hint">사례품 발송 외 다른 목적으로는 사용되지 않습니다.</p>
        <div class="register-grid">
          <label>
            <span>이름 (수령자명) *</span>
            <input type="text" id="reg-reward-name" value="${this.escape(d.reward_name)}" placeholder="응답자 본인 또는 수령자" />
          </label>
          <label>
            <span>휴대폰 번호 *</span>
            <input type="tel" id="reg-reward-phone" value="${this.escape(d.reward_phone)}" placeholder="010-0000-0000" />
          </label>
        </div>
      </div>
    ` : `
      <div class="register-skip-reward">
        사례품 발송 동의를 하지 않으셨으므로 이름·휴대폰 번호는 수집하지 않습니다.
        (이전 단계에서 동의를 추가하실 수 있습니다.)
      </div>
    `;

    this.container.innerHTML = `
      <div class="survey-container">
        <div class="register-stepper">
          <span class="step done">①</span><span class="step-line"></span>
          <span class="step done">②</span><span class="step-line"></span>
          <span class="step active">③</span>
        </div>

        <div class="register-card">
          <h2>응답자 정보 입력</h2>
          <p class="register-hint">
            <span class="required">*</span> 표시는 필수 항목입니다.
            응답 분석에는 <strong>분류 정보(지자체 유형·소속·담당업무 등)</strong>만 활용되며,
            이메일은 응답 완료 안내 메일 발송에만 사용됩니다.
          </p>

          <div class="register-section">
            <h3>① 응답 완료 안내 받을 이메일 <span class="register-section-tag">(필수)</span></h3>
            <p class="register-hint">제출 직후 이 주소로 응답 확인 링크가 자동 발송됩니다. 분석·통계 처리에는 사용되지 않습니다.</p>
            <div class="register-grid">
              <label class="full">
                <span>이메일 *</span>
                <input type="email" id="reg-email" value="${this.escape(d.email)}" placeholder="example@example.go.kr" />
              </label>
            </div>
          </div>

          <div class="register-section">
            <h3>② 소속 기관 및 담당업무 <span class="register-section-tag">(분류 통계용 — 동의 불요)</span></h3>
            <p class="register-hint">통계 분석을 위한 분류 정보입니다. 보고서·논문에서 개별 응답자나 기관이 그대로 노출되지 않습니다.</p>
            <div class="register-grid">
              <label class="full">
                <span>지자체 유형 *</span>
                <div class="register-radio-row">
                  <label class="register-radio">
                    <input type="radio" name="reg-category" value="광역자치단체" ${d.category === '광역자치단체' ? 'checked' : ''}>
                    <span>광역자치단체 (시·도)</span>
                  </label>
                  <label class="register-radio">
                    <input type="radio" name="reg-category" value="기초자치단체" ${d.category === '기초자치단체' ? 'checked' : ''}>
                    <span>기초자치단체 (시·군·구)</span>
                  </label>
                </div>
              </label>
              <label class="full">
                <span>지자체명 * <small>(예: 서울특별시 / 수원시 / 대구광역시 동구)</small></span>
                <input type="text" id="reg-org" value="${this.escape(d.org)}" placeholder="시·도 / 시·군·구 명칭" />
              </label>
              <label>
                <span>부서</span>
                <input type="text" id="reg-dept" value="${this.escape(d.dept)}" placeholder="예) 재산관리과" />
              </label>
              <label>
                <span>팀</span>
                <input type="text" id="reg-team" value="${this.escape(d.team)}" placeholder="예) 재산정책팀" />
              </label>
              <label>
                <span>직위</span>
                <input type="text" id="reg-position" value="${this.escape(d.position)}" placeholder="예) 팀장, 주무관" />
              </label>
              <label>
                <span>직급</span>
                <input type="text" id="reg-rank" value="${this.escape(d.rank)}" placeholder="예) 행정5급" />
              </label>
              <label class="full">
                <span>담당업무</span>
                <input type="text" id="reg-duty" value="${this.escape(d.duty)}" placeholder="예) 청사 유지관리 총괄" />
              </label>
            </div>
          </div>

          ${rewardBlock}

          ${errHtml}

          <div class="register-actions">
            <button class="btn btn-prev" id="btn-reg-back">← 이전</button>
            <button class="btn btn-next" id="btn-reg-submit" ${this.regSubmitting ? 'disabled' : ''}>
              ${this.regSubmitting ? '등록 중…' : '등록하고 설문 시작 →'}
            </button>
          </div>
          ${submittingHtml}
        </div>
      </div>
    `;
    this.bindRegisterInfoEvents();
  }

  bindRegisterInfoEvents() {
    const bind = (id, key, isCheckbox = false) => {
      const el = this.container.querySelector(`#${id}`);
      if (!el) return;
      el.addEventListener('input', () => {
        this.regDraft[key] = isCheckbox ? el.checked : el.value;
        this.saveRegisterDraft();
      });
    };
    bind('reg-email', 'email');
    bind('reg-org', 'org');
    bind('reg-dept', 'dept');
    bind('reg-team', 'team');
    bind('reg-position', 'position');
    bind('reg-rank', 'rank');
    bind('reg-duty', 'duty');
    bind('reg-reward-name', 'reward_name');
    bind('reg-reward-phone', 'reward_phone');

    this.container.querySelectorAll('input[name="reg-category"]').forEach(radio => {
      radio.addEventListener('change', (e) => {
        if (e.target.checked) {
          this.regDraft.category = e.target.value;
          this.saveRegisterDraft();
        }
      });
    });

    this.container.querySelector('#btn-reg-back')?.addEventListener('click', () => {
      this.regStep = REG_STEP.CONSENT;
      this.regError = '';
      this.render();
    });
    this.container.querySelector('#btn-reg-submit')?.addEventListener('click', () => {
      this.submitRegistration();
    });
  }

  async submitRegistration() {
    const d = this.regDraft;
    const errors = [];
    const email = (d.email || '').trim();
    if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) errors.push('올바른 이메일을 입력해 주십시오.');
    if (!d.consent_pi) errors.push('이메일 수집·이용에 동의해 주십시오 (필수).');
    if (d.category !== '광역자치단체' && d.category !== '기초자치단체') errors.push('지자체 유형(광역/기초)을 선택해 주십시오.');
    if (!(d.org || '').trim()) errors.push('지자체명을 입력해 주십시오.');
    if (d.consent_reward) {
      if (!(d.reward_name || '').trim()) errors.push('사례품 수령자명을 입력해 주십시오.');
      const phone = (d.reward_phone || '').trim();
      if (!phone || !/^[\d\-\s+()]{9,}$/.test(phone)) errors.push('올바른 휴대폰 번호를 입력해 주십시오.');
    }
    if (errors.length) {
      this.regError = errors.join(' / ');
      this.render();
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    this.regError = '';
    this.regSubmitting = true;
    this.render();

    try {
      const payload = {
        email: email,
        org: (d.org || '').trim(),
        category: d.category,
        dept: (d.dept || '').trim(),
        team: (d.team || '').trim(),
        position: (d.position || '').trim(),
        rank: (d.rank || '').trim(),
        duty: (d.duty || '').trim(),
        consent_pi: !!d.consent_pi,
        consent_reward: !!d.consent_reward,
        reward_name: d.consent_reward ? (d.reward_name || '').trim() : '',
        reward_phone: d.consent_reward ? (d.reward_phone || '').trim() : '',
      };
      const res = await fetch(`${API_BASE}/api/survey/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `등록 실패 (${res.status})`);
      }
      const data = await res.json();
      this.clearRegisterDraft();
      // 발급된 토큰으로 이동 — 페이지 reload하여 토큰 인증 흐름 진입.
      // just_registered=1 플래그로 인트로에 "이 URL 북마크 안내" 배너 한 번 표시.
      const url = new URL(window.location.href);
      url.searchParams.set('token', data.token);
      url.searchParams.set('just_registered', '1');
      window.location.href = url.toString();
    } catch (e) {
      this.regError = e.message || '등록 중 오류가 발생했습니다. 잠시 후 다시 시도해 주십시오.';
      this.regSubmitting = false;
      this.render();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }

  // ── Access Denied ──
  renderAccessDenied() {
    const m = SURVEY_META;
    this.container.innerHTML = `
      <div class="survey-container">
        <div class="access-denied">
          <div class="access-denied-icon">
            <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.6">
              <circle cx="12" cy="12" r="9"></circle>
              <line x1="5.6" y1="5.6" x2="18.4" y2="18.4"></line>
            </svg>
          </div>
          <h1>접근 권한이 없습니다</h1>
          <p class="access-denied-msg">
            본 설문은 사전에 발송된 개별 링크를 통해서만 참여할 수 있습니다.<br/>
            이메일로 수신한 링크를 다시 확인하시거나, 아래 연락처로 문의해 주십시오.
          </p>
          <div class="access-denied-meta">
            <dl>
              <dt>조사기관</dt><dd>${m.institution}</dd>
              <dt>연구책임</dt><dd>${m.researcher}</dd>
              <dt>문의</dt><dd>${m.contact}</dd>
            </dl>
          </div>
        </div>
      </div>
    `;
  }

  // ── Resubmit Choice (이미 제출한 토큰 재접근) ──
  renderResubmitChoice() {
    const p = this.participant || {};
    const submittedStr = this.submittedAt ? this.formatDateTime(this.submittedAt) : '';
    const updatedStr = this.updatedAt ? this.formatDateTime(this.updatedAt) : '';

    this.container.innerHTML = `
      <div class="survey-container">
        <div class="resubmit-choice">
          <div class="resubmit-badge">제출 완료</div>
          <h1>이미 응답을 제출하셨습니다</h1>
          <div class="resubmit-meta">
            <dl>
              <dt>응답자</dt><dd>${this.escape(p.name || '-')}${p.org ? ` · ${this.escape(p.org)}` : ''}</dd>
              <dt>최초 제출</dt><dd>${submittedStr || '-'}</dd>
              ${updatedStr ? `<dt>최근 수정</dt><dd>${updatedStr}</dd>` : ''}
            </dl>
          </div>
          <p class="resubmit-msg">
            응답 내용을 <strong>수정</strong>하시거나, 제출한 응답을 <strong>확인</strong>만 하실 수 있습니다.
          </p>
          <div class="resubmit-actions">
            <button class="btn btn-next" id="btn-edit-mode">응답 수정하기</button>
            <button class="btn btn-prev" id="btn-view-mode">내 응답 확인 (읽기전용)</button>
          </div>
        </div>
      </div>
    `;
    this.container.querySelector('#btn-edit-mode').addEventListener('click', () => {
      this.editMode = EDIT_MODE.EDIT;
      this.gate = GATE.OPEN;
      this.currentPage = 0;
      this.render();
    });
    this.container.querySelector('#btn-view-mode').addEventListener('click', () => {
      this.gate = GATE.REVIEW;
      this.render();
    });
  }

  // ── 읽기전용 응답 리뷰 (?review=1 또는 RESUBMIT_CHOICE에서 진입) ──
  renderReview() {
    const m = SURVEY_META;
    const p = this.participant || {};
    const submittedStr = this.submittedAt ? this.formatDateTime(this.submittedAt) : '-';
    const updatedStr = this.updatedAt ? this.formatDateTime(this.updatedAt) : '';

    this.updateVisibleSections();

    const sectionsHtml = this.visibleSections.map(s => {
      const questionsHtml = s.questions.map(q => {
        if (this.shouldSkipQuestion(q)) return '';
        if (q.type === Q_TYPE.SUB_QUESTIONS) {
          const subs = q.subQuestions.map(sq => `
            <div class="rv-sub">
              <div class="rv-sub-label">${this.escape(sq.label)}</div>
              <div class="rv-sub-answer">${this.formatAnswerHtml(sq, this.responses[sq.id])}</div>
            </div>
          `).join('');
          return `
            <div class="rv-q">
              <div class="rv-q-id">${q.id.replace(/([A-Z]+)(\d)/, '$1-$2')}</div>
              <div class="rv-q-text">${this.escape(q.text)}</div>
              <div class="rv-sub-group">${subs}</div>
            </div>
          `;
        }
        return `
          <div class="rv-q">
            <div class="rv-q-id">${q.id.replace(/([A-Z]+)(\d)/, '$1-$2')}</div>
            <div class="rv-q-text">${this.escape(q.text)}</div>
            <div class="rv-answer">${this.formatAnswerHtml(q, this.responses[q.id])}</div>
          </div>
        `;
      }).join('');
      return `
        <section class="rv-section">
          <div class="rv-section-header">
            <span class="rv-section-tag">${this.escape(s.tag || '')}</span>
            <h3>${this.escape(s.title)}</h3>
            ${s.subtitle ? `<p class="rv-section-subtitle">${this.escape(s.subtitle)}</p>` : ''}
          </div>
          ${questionsHtml}
        </section>
      `;
    }).join('');

    const participantBlock = `
      <div class="rv-participant">
        <h3>응답자 정보</h3>
        <dl>
          <dt>이름</dt><dd>${this.escape(p.name || '-')}</dd>
          <dt>이메일</dt><dd>${this.escape(p.email || '-')}</dd>
          <dt>소속</dt><dd>${this.escape(p.org || '-')}</dd>
          ${p.dept ? `<dt>부서</dt><dd>${this.escape(p.dept)}</dd>` : ''}
          ${p.team ? `<dt>팀</dt><dd>${this.escape(p.team)}</dd>` : ''}
          ${p.position ? `<dt>직위</dt><dd>${this.escape(p.position)}</dd>` : ''}
          ${p.rank ? `<dt>직급</dt><dd>${this.escape(p.rank)}</dd>` : ''}
          ${p.duty ? `<dt>담당업무</dt><dd>${this.escape(p.duty)}</dd>` : ''}
          ${p.phone ? `<dt>연락처</dt><dd>${this.escape(p.phone)}</dd>` : ''}
        </dl>
      </div>
    `;

    this.container.innerHTML = `
      <div class="survey-container">
        <div class="rv-header">
          <div class="rv-badge">제출 완료 · 응답 확인</div>
          <div class="rv-institution">${m.institution}</div>
          <h1 class="rv-title">${m.title}</h1>
          <div class="rv-meta">
            <dl>
              <dt>최초 제출</dt><dd>${submittedStr}</dd>
              ${updatedStr ? `<dt>최근 수정</dt><dd>${updatedStr}</dd>` : ''}
            </dl>
          </div>
          <p class="rv-notice">
            아래 내용은 귀하께서 제출하신 응답입니다. 본 페이지는 <strong>읽기전용</strong>이며 응답 수정은 불가합니다.
            수정이 필요하신 경우 <strong>${this.escape(m.contact)}</strong>로 연락 주십시오.
          </p>
        </div>

        ${participantBlock}

        ${sectionsHtml}

        <div class="rv-footer">
          <p>본 실태조사에 응답해 주셔서 진심으로 감사드립니다.</p>
          <p class="rv-footer-sub">「청사 관리에 관한 법률(가칭)」 제정을 위한 정책 기초 자료로 활용됩니다.</p>
        </div>
      </div>
    `;
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  formatAnswerHtml(q, val) {
    if (val === undefined || val === null || val === '') {
      return '<span class="rv-empty">(무응답)</span>';
    }
    if (q.type === Q_TYPE.SINGLE || q.type === Q_TYPE.SINGLE_WITH_OTHER) {
      if (val === 'other') {
        const other = this.responses[q.id + '_other'] || '';
        return `<span class="rv-chip rv-chip-other">기타: ${this.escape(other)}</span>`;
      }
      const idx = typeof val === 'number' ? val : parseInt(val, 10);
      const opt = q.options?.[idx];
      return opt ? `<span class="rv-chip">${this.escape(opt)}</span>` : `<code>${this.escape(String(val))}</code>`;
    }
    if (q.type === Q_TYPE.MULTI || q.type === Q_TYPE.MULTI_WITH_OTHER ||
        q.type === Q_TYPE.MULTI_LIMIT || q.type === Q_TYPE.MULTI_LIMIT_OTHER) {
      if (!Array.isArray(val)) return `<code>${this.escape(JSON.stringify(val))}</code>`;
      if (val.length === 0) return '<span class="rv-empty">(선택 없음)</span>';
      return val.map(v => {
        if (v === 'other') {
          const other = this.responses[q.id + '_other'] || '';
          return `<span class="rv-chip rv-chip-other">기타: ${this.escape(other)}</span>`;
        }
        const idx = typeof v === 'number' ? v : parseInt(v, 10);
        const opt = q.options?.[idx];
        return `<span class="rv-chip">${this.escape(opt || String(v))}</span>`;
      }).join(' ');
    }
    if (q.type === Q_TYPE.NUMBER_TABLE) {
      if (typeof val !== 'object') return `<code>${this.escape(JSON.stringify(val))}</code>`;
      const fmt = (n) => new Intl.NumberFormat('ko').format(Number(n) || 0);
      const header = `<tr><th>연도</th>${q.columns.map(c => `<th>${this.escape(c.label)}</th>`).join('')}<th>합계</th></tr>`;
      const rows = q.rows.map(r => {
        const rv = val[r.id];
        if (rv === 'unknown') {
          return `<tr><td>${this.escape(r.label)}</td><td colspan="${q.columns.length + 1}" class="rv-empty">모름/해당없음</td></tr>`;
        }
        if (!rv || typeof rv !== 'object') {
          return `<tr><td>${this.escape(r.label)}</td><td colspan="${q.columns.length + 1}" class="rv-empty">(무응답)</td></tr>`;
        }
        const cells = q.columns.map(c => `<td class="rv-num">${fmt(rv[c.id])}</td>`).join('');
        const sum = q.columns.reduce((a, c) => a + (Number(rv[c.id]) || 0), 0);
        return `<tr><td>${this.escape(r.label)}</td>${cells}<td class="rv-num rv-sum">${fmt(sum)}</td></tr>`;
      }).join('');
      const unitNote = q.unit ? `<div class="rv-unit">단위: ${this.escape(q.unit)}</div>` : '';
      return `${unitNote}<table class="rv-number-table"><thead>${header}</thead><tbody>${rows}</tbody></table>`;
    }
    if (q.type === Q_TYPE.LIKERT_TABLE) {
      if (typeof val !== 'object') return `<code>${this.escape(JSON.stringify(val))}</code>`;
      const rows = q.items.map((item, i) => {
        const v = val[i];
        const label = v ? (q.scaleLabels?.[v - 1] || v) : '(무응답)';
        const cls = v ? 'rv-likert-val' : 'rv-empty';
        return `<tr><td>${this.escape(item)}</td><td class="${cls}">${this.escape(String(label))}</td></tr>`;
      }).join('');
      return `<table class="rv-likert-table"><tbody>${rows}</tbody></table>`;
    }
    if (q.type === Q_TYPE.TEXT) {
      const s = String(val);
      if (s === '(모름/해당없음)') return '<span class="rv-empty">모름/해당없음</span>';
      return `<div class="rv-text">${this.escape(s).replace(/\n/g, '<br>')}</div>`;
    }
    return `<code>${this.escape(JSON.stringify(val))}</code>`;
  }

  // ── Status Bar (공통 상단) ──
  renderStatusBar() {
    let status, statusClass;
    if (this.submitted && this.editMode === EDIT_MODE.EDIT) {
      status = '수정 중';
      statusClass = 'status-editing';
    } else if (this.submitted) {
      status = '제출 완료';
      statusClass = 'status-done';
    } else {
      status = '미제출';
      statusClass = 'status-pending';
    }

    const submittedInfo = this.submittedAt
      ? `<span class="status-time">제출: ${this.formatDateTime(this.submittedAt)}</span>`
      : '';
    const updatedInfo = this.updatedAt
      ? `<span class="status-time">수정: ${this.formatDateTime(this.updatedAt)}</span>`
      : '';

    return `
      <div class="status-info-bar">
        <div class="status-info-inner">
          <span class="status-badge ${statusClass}">${status}</span>
          <div class="status-times">
            ${submittedInfo}
            ${updatedInfo}
          </div>
        </div>
      </div>
    `;
  }

  // ── Participant Info Card ──
  renderParticipantCard() {
    const p = this.participant;
    if (!p) return '';

    if (this.editingParticipant) {
      const errHtml = this.participantFormError
        ? `<p class="participant-error">${this.escape(this.participantFormError)}</p>`
        : '';
      return `
        <div class="participant-card editing">
          <div class="participant-card-header">
            <h3>내 정보 수정</h3>
            <p class="participant-hint">부서 이동·인사 변동이 있으셨다면 이 화면에서 갱신해 주십시오.</p>
          </div>
          <div class="participant-form">
            <label>
              <span>이름</span>
              <input type="text" id="p-name" value="${this.escape(p.name || '')}" />
            </label>
            <label>
              <span>이메일</span>
              <input type="email" id="p-email" value="${this.escape(p.email || '')}" />
            </label>
            <label>
              <span>소속 (시·도 / 시·군·구)</span>
              <input type="text" id="p-org" value="${this.escape(p.org || '')}" />
            </label>
            <label>
              <span>부서명</span>
              <input type="text" id="p-dept" value="${this.escape(p.dept || '')}" placeholder="예) 재산관리과" />
            </label>
            <label>
              <span>팀명</span>
              <input type="text" id="p-team" value="${this.escape(p.team || '')}" placeholder="예) 재산정책팀 (없으면 비워두세요)" />
            </label>
            <label>
              <span>직위</span>
              <input type="text" id="p-position" value="${this.escape(p.position || '')}" placeholder="예) 팀장, 주무관" />
            </label>
            <label>
              <span>직급</span>
              <input type="text" id="p-rank" value="${this.escape(p.rank || '')}" placeholder="예) 행정5급, 지방행정서기관" />
            </label>
            <label>
              <span>담당업무</span>
              <input type="text" id="p-duty" value="${this.escape(p.duty || '')}" placeholder="예) 청사 유지관리 총괄" />
            </label>
            <label>
              <span>사무실 번호</span>
              <input type="tel" id="p-phone" value="${this.escape(p.phone || '')}" placeholder="02-0000-0000" />
            </label>
          </div>
          ${errHtml}
          <div class="participant-actions">
            <button class="btn btn-prev" id="btn-p-cancel">취소</button>
            <button class="btn btn-next" id="btn-p-save">저장</button>
          </div>
        </div>
      `;
    }

    return `
      <div class="participant-card">
        <div class="participant-card-header">
          <h3>내 정보</h3>
          <button class="btn-link" id="btn-p-edit">수정</button>
        </div>
        <p class="participant-hint">아래 정보는 응답자 DB에서 미리 채워둔 값입니다. 변경된 사항이 있으면 <strong>수정</strong> 버튼으로 갱신해 주십시오.</p>
        <dl class="participant-info">
          <dt>이름</dt><dd>${this.escape(p.name || '-')}</dd>
          <dt>이메일</dt><dd>${this.escape(p.email || '-')}</dd>
          <dt>소속</dt><dd>${this.escape(p.org || '-')}</dd>
          <dt>부서</dt><dd>${this.escape(p.dept || '-')}</dd>
          <dt>팀</dt><dd>${this.escape(p.team || '-')}</dd>
          <dt>직위</dt><dd>${this.escape(p.position || '-')}</dd>
          <dt>직급</dt><dd>${this.escape(p.rank || '-')}</dd>
          <dt>담당업무</dt><dd>${this.escape(p.duty || '-')}</dd>
          <dt>사무실 번호</dt><dd>${this.escape(p.phone || '-')}</dd>
          <dt>구분</dt><dd class="readonly">${this.escape(p.category || '-')} <span class="hint">(사전 분류)</span></dd>
        </dl>
      </div>
    `;
  }

  bindParticipantEvents() {
    this.container.querySelector('#btn-p-edit')?.addEventListener('click', () => {
      this.editingParticipant = true;
      this.participantFormError = '';
      this.render();
    });
    this.container.querySelector('#btn-p-cancel')?.addEventListener('click', () => {
      this.editingParticipant = false;
      this.participantFormError = '';
      this.render();
    });
    this.container.querySelector('#btn-p-save')?.addEventListener('click', () => {
      this.saveParticipant();
    });
  }

  async saveParticipant() {
    const val = (id) => this.container.querySelector(`#${id}`)?.value.trim() ?? '';

    const payload = {
      name: val('p-name'),
      email: val('p-email'),
      org: val('p-org'),
      phone: val('p-phone'),
      dept: val('p-dept'),
      team: val('p-team'),
      position: val('p-position'),
      rank: val('p-rank'),
      duty: val('p-duty'),
    };

    if (!payload.name) {
      this.participantFormError = '이름을 입력해 주십시오.';
      this.render();
      return;
    }
    if (!payload.email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(payload.email)) {
      this.participantFormError = '올바른 이메일을 입력해 주십시오.';
      this.render();
      return;
    }

    const saveBtn = this.container.querySelector('#btn-p-save');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '저장 중…'; }

    try {
      const res = await fetch(`${API_BASE}/api/survey/${this.token}/participant`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `저장 실패 (${res.status})`);
      }
      const data = await res.json();
      this.participant = { ...this.participant, ...data.participant };
      this.editingParticipant = false;
      this.participantFormError = '';
      this.render();
    } catch (err) {
      this.participantFormError = err.message || '저장 중 오류가 발생했습니다.';
      if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '저장'; }
      this.render();
    }
  }

  // ── Intro ──
  renderIntro() {
    const m = SURVEY_META;
    const statusBar = this.renderStatusBar();
    const participantCard = this.renderParticipantCard();
    const startLabel = this.submitted && this.editMode === EDIT_MODE.EDIT
      ? '응답 수정 시작하기'
      : '설문 시작하기';

    // 등록 직후 한 번 표시되는 토큰 URL 북마크 안내. 닫으면 just_registered 파라미터 제거.
    const bookmarkBanner = this.justRegistered ? `
      <div class="bookmark-banner">
        <div class="bookmark-banner-icon">🔖</div>
        <div class="bookmark-banner-body">
          <strong>이 페이지의 URL을 북마크해 주세요.</strong>
          <p>응답 작성 도중 자리를 비우셨다가 다시 돌아오실 때 사용하시는 <strong>고유 응답 링크</strong>입니다.
             제출 직후 입력하신 이메일로 응답 확인 링크가 자동 발송됩니다.</p>
          <button class="bookmark-banner-close" id="btn-bookmark-close" aria-label="닫기">×</button>
        </div>
      </div>
    ` : '';

    this.container.innerHTML = `
      ${statusBar}
      <div class="progress-bar-wrap"><div class="progress-bar-inner">
        <div class="progress-track"><div class="progress-fill" style="width:0%"></div></div>
        <span class="progress-label">0%</span>
      </div></div>
      <div class="survey-container with-status-bar">
        <div class="survey-header">
          <div class="institution">${m.institution}</div>
          <h1>${m.title}</h1>
          <div class="subtitle">${m.subtitle}</div>
        </div>

        ${bookmarkBanner}

        ${participantCard}

        <div class="intro-card">
          <h2>조사 목적</h2>
          <p>본 조사는 국가 공공자산인 지방자치단체 청사의 체계적 관리를 위한 법적 기반을 마련하고, 중앙·지방 간 관리 격차를 해소하기 위한 정책 기초 자료 확보를 목적으로 합니다. 「청사 관리에 관한 법률(가칭)」 제정을 위한 실태조사의 일환으로 수행됩니다.</p>
        </div>

        <div class="intro-card">
          <h2>응답 안내</h2>
          <ul style="margin-top:12px">
            <li>응답 중간에 <strong>자동 저장</strong>되며, 링크를 다시 열면 이어서 작성할 수 있습니다.</li>
            <li>상단에 진행률이 표시됩니다.</li>
            <li>정확한 응답을 위해 <strong>인력 현황·5년치 예산·자체 실태조사 항목</strong> 등 자료를 미리 준비해 주시면 좋습니다.</li>
            <li>모든 응답은 「통계법」 제33조(비밀의 보호)에 따라 통계 목적으로만 사용됩니다.</li>
          </ul>
          <dl class="intro-meta">
            <dt>소요 시간</dt><dd>${m.duration}</dd>
            <dt>비밀보장</dt><dd>개별 응답 내용은 엄격히 보호, 공표 시 익명 처리</dd>
            <dt>담당자</dt><dd>${m.researcher} (${m.contact})</dd>
          </dl>
        </div>

        <button class="btn-start" id="btn-start">${startLabel}</button>
      </div>
    `;
    this.bindParticipantEvents();
    this.container.querySelector('#btn-start')?.addEventListener('click', () => {
      this.currentPage = 1;
      this.render();
    });
    this.container.querySelector('#btn-bookmark-close')?.addEventListener('click', () => {
      this.justRegistered = false;
      const url = new URL(window.location.href);
      url.searchParams.delete('just_registered');
      window.history.replaceState({}, '', url.toString());
      this.render();
    });
  }

  // ── Section ──
  renderSection(section) {
    const pct = Math.round((this.currentPage / (this.visibleSections.length + 1)) * 100);
    const isLast = this.currentPage === this.visibleSections.length;
    const statusBar = this.renderStatusBar();
    const submitLabel = this.submitted && this.editMode === EDIT_MODE.EDIT ? '수정 내용 제출' : '제출하기';

    let html = `
      ${statusBar}
      <div class="progress-bar-wrap"><div class="progress-bar-inner">
        <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
        <span class="progress-label">${pct}%</span>
      </div></div>
      <div class="survey-container with-status-bar">
        <div class="section">
          <div class="section-header">
            <span class="section-tag">${section.tag}</span>
            <h2>${section.title}</h2>
            <p class="section-subtitle">${section.subtitle}</p>
          </div>
    `;

    for (const q of section.questions) {
      html += this.renderQuestion(q);
      if (this.isReviewer()) {
        if (q.type === Q_TYPE.SUB_QUESTIONS) {
          // sub-question별 스레드는 renderSubQuestions 내부에서 이미 sub-item 바로 아래에 주입됨.
          // parent qid 스레드는 (마이그레이션 등으로) 코멘트가 있을 때만 노출 (빈 폼 중복 방지).
          const parentCount = (this.threads[q.id] || []).length;
          if (parentCount > 0) {
            html += this.renderReviewerThread(q.id);
          }
        } else {
          html += this.renderReviewerThread(q.id);
        }
      }
    }

    html += `</div></div>`;
    const reviewerSkipBtn = this.isReviewer() && !isLast
      ? '<button class="btn btn-skip" id="btn-skip" title="연구진 전용 — 필수 응답 검증 없이 다음으로">⏭ 건너뛰기 (검토용)</button>'
      : '';
    const reviewerForceSubmit = this.isReviewer() && isLast
      ? '<button class="btn btn-skip" id="btn-force-submit" title="연구진 전용 — 필수 응답 검증 없이 제출">⏭ 강제 제출 (검토용)</button>'
      : '';

    html += `
      <div class="nav-bar"><div class="nav-inner">
        <button class="btn btn-prev" id="btn-prev">&larr; 이전</button>
        ${reviewerSkipBtn}${reviewerForceSubmit}
        ${isLast
          ? `<button class="btn btn-submit" id="btn-next">${submitLabel}</button>`
          : '<button class="btn btn-next" id="btn-next">다음 &rarr;</button>'
        }
      </div></div>
    `;

    this.container.innerHTML = html;
    this.bindEvents(section);
    this.restoreValues(section);
  }

  shouldSkipQuestion(q) {
    // Q1(지자체 유형)은 등록·import 시점에 category로 이미 결정되므로 화면에서 생략한다.
    if (q.id === 'Q1') {
      const cat = this.participant?.category;
      if (cat === '광역자치단체' || cat === '기초자치단체') return true;
    }
    return false;
  }

  renderQuestion(q) {
    if (this.shouldSkipQuestion(q)) return '';
    if (q.type === Q_TYPE.SUB_QUESTIONS) {
      return this.renderSubQuestions(q);
    }

    let inner = '';
    const noteHtml = q.note ? `<p class="question-note">${q.note}</p>` : '';

    switch (q.type) {
      case Q_TYPE.SINGLE:
      case Q_TYPE.SINGLE_WITH_OTHER:
        inner = this.renderOptions(q, 'radio', q.type === Q_TYPE.SINGLE_WITH_OTHER);
        break;
      case Q_TYPE.MULTI:
      case Q_TYPE.MULTI_LIMIT:
        inner = this.renderOptions(q, 'checkbox');
        break;
      case Q_TYPE.MULTI_WITH_OTHER:
      case Q_TYPE.MULTI_LIMIT_OTHER:
        inner = this.renderOptions(q, 'checkbox', true);
        break;
      case Q_TYPE.LIKERT_TABLE:
        inner = this.renderLikertTable(q);
        break;
      case Q_TYPE.NUMBER_TABLE:
        inner = this.renderNumberTable(q);
        break;
      case Q_TYPE.TEXT:
        inner = this.renderTextInput(q);
        break;
    }

    return `
      <div class="question-block" data-qid="${q.id}">
        <div class="question-label">
          <span class="question-id">${q.id.replace(/([A-Z]+)(\d)/, '$1-$2')}</span>
          <span class="question-text">${q.text}</span>
        </div>
        ${noteHtml}
        ${inner}
        <p class="question-error" data-error="${q.id}"></p>
      </div>
    `;
  }

  renderOptions(q, inputType, hasOther = false) {
    let html = `<div class="option-list" data-qid="${q.id}" data-type="${inputType}">`;
    const name = q.id;
    q.options.forEach((opt, i) => {
      html += `
        <label class="option-item" data-index="${i}">
          <input type="${inputType}" name="${name}" value="${i}" />
          <span class="option-text">${opt}</span>
        </label>
      `;
    });
    if (hasOther) {
      html += `
        <label class="option-item other-row" data-index="other">
          <input type="${inputType}" name="${name}" value="other" />
          <span class="option-text">${q.otherLabel || '기타'}:</span>
          <input type="text" class="other-text" data-qid="${q.id}_other" placeholder="직접 입력" />
        </label>
      `;
    }
    html += '</div>';
    return html;
  }

  renderLikertTable(q) {
    let html = '<div class="likert-table-wrap"><table class="likert-table" data-qid="' + q.id + '">';
    html += '<thead><tr><th></th>';
    q.scaleLabels.forEach((l, i) => { html += `<th>${i + 1}<br><span style="font-weight:400">${l}</span></th>`; });
    html += '</tr></thead><tbody>';
    q.items.forEach((item, idx) => {
      html += `<tr data-row="${idx}">`;
      html += `<td><span class="item-number">(${idx + 1})</span>${item}</td>`;
      for (let v = 1; v <= q.scaleLabels.length; v++) {
        html += `<td><input type="radio" class="likert-radio" name="${q.id}_${idx}" value="${v}" /></td>`;
      }
      html += '</tr>';
    });
    html += '</tbody></table></div>';
    return html;
  }

  renderNumberTable(q) {
    const unit = q.unit ? `<div class="number-table-unit">(단위: ${q.unit})</div>` : '';

    // Header rows
    let headTop = '<tr><th rowspan="2" class="th-row-label">연도</th>';
    let headBottom = '<tr>';

    if (q.columnGroups && q.columnGroups.length > 0) {
      for (const g of q.columnGroups) {
        if (g.colIds.length > 1) {
          headTop += `<th colspan="${g.colIds.length}">${g.label}</th>`;
          for (const cid of g.colIds) {
            const c = q.columns.find(x => x.id === cid);
            headBottom += `<th>${c?.label || cid}</th>`;
          }
        } else {
          headTop += `<th rowspan="2">${g.label}</th>`;
        }
      }
    } else {
      for (const c of q.columns) {
        headTop += `<th rowspan="2">${c.label}</th>`;
      }
    }

    if (q.showRowSum) headTop += '<th rowspan="2" class="th-sum">합계</th>';
    if (q.allowUnknownPerRow) headTop += '<th rowspan="2" class="th-unknown">모름/<br>해당없음</th>';
    headTop += '</tr>';
    headBottom += '</tr>';

    // Body
    let body = '';
    for (const r of q.rows) {
      body += `<tr data-row="${r.id}">`;
      body += `<th class="row-label">${r.label}</th>`;
      for (const c of q.columns) {
        body += `<td><input type="number" class="cell-input" data-q="${q.id}" data-r="${r.id}" data-c="${c.id}" step="1" min="0" placeholder="0" /></td>`;
      }
      if (q.showRowSum) body += `<td class="cell-sum" data-r="${r.id}">0</td>`;
      if (q.allowUnknownPerRow) {
        body += `<td class="cell-unknown"><input type="checkbox" class="row-unknown-toggle" data-q="${q.id}" data-r="${r.id}" /></td>`;
      }
      body += '</tr>';
    }

    return `
      <div class="number-table-wrap" data-qid="${q.id}">
        ${unit}
        <table class="number-table">
          <thead>${headTop}${headBottom}</thead>
          <tbody>${body}</tbody>
        </table>
      </div>
    `;
  }

  renderTextInput(q) {
    const isIdCode = q.id === 'ID_CODE';
    const cls = isIdCode ? 'text-input id-code-input' : 'text-input';
    if (isIdCode) {
      return `<input type="text" class="${cls}" data-qid="${q.id}" placeholder="${q.placeholder || ''}" maxlength="10" />`;
    }
    return `<textarea class="${cls}" data-qid="${q.id}" placeholder="${q.placeholder || ''}" rows="4"></textarea>`;
  }

  renderSubQuestions(q) {
    let html = `
      <div class="question-block" data-qid="${q.id}">
        <div class="question-label">
          <span class="question-id">${q.id.replace(/([A-Z]+)(\d)/, '$1-$2')}</span>
          <span class="question-text">${q.text}</span>
        </div>
        <div class="sub-question-group">
    `;
    for (const sq of q.subQuestions) {
      const noteHtml = sq.note ? `<p class="sub-question-note">${sq.note}</p>` : '';
      let inner = '';
      if (sq.type === Q_TYPE.SINGLE) {
        inner = this.renderOptions(sq, 'radio');
      } else if (sq.type === Q_TYPE.SINGLE_WITH_OTHER) {
        inner = this.renderOptions(sq, 'radio', true);
      } else if (sq.type === Q_TYPE.MULTI_WITH_OTHER || sq.type === Q_TYPE.MULTI_LIMIT_OTHER) {
        inner = this.renderOptions(sq, 'checkbox', true);
      } else if (sq.type === Q_TYPE.MULTI) {
        inner = this.renderOptions(sq, 'checkbox');
      } else if (sq.type === Q_TYPE.TEXT) {
        if (sq.allowUnknown) {
          inner = `
            <div class="sub-text-row">
              <input type="text" class="text-input sub-text" data-qid="${sq.id}" placeholder="${sq.placeholder || ''}" />
              <label class="unknown-check"><input type="checkbox" class="unknown-toggle" data-target="${sq.id}" /> <span>모름/해당없음</span></label>
            </div>
          `;
        } else {
          inner = `<input type="text" class="text-input sub-text" data-qid="${sq.id}" placeholder="${sq.placeholder || ''}" />`;
        }
      }
      html += `
        <div class="sub-question" data-qid="${sq.id}">
          <div class="sub-question-label">${sq.label}</div>
          ${noteHtml}
          ${inner}
          <p class="question-error" data-error="${sq.id}"></p>
        </div>
      `;
      if (this.isReviewer()) {
        html += this.renderReviewerThread(sq.id);
      }
    }
    html += '</div></div>';
    return html;
  }

  // ── Event Binding ──
  bindEvents(section) {
    this.container.querySelector('#btn-prev')?.addEventListener('click', () => {
      this.currentPage--;
      if (this.currentPage < 0) this.currentPage = 0;
      this.render();
    });

    this.container.querySelector('#btn-next')?.addEventListener('click', () => {
      if (this.validateSection(section)) {
        this.currentPage++;
        this.updateVisibleSections();
        this.render();
      }
    });

    this.container.querySelector('#btn-skip')?.addEventListener('click', () => {
      this.currentPage++;
      this.updateVisibleSections();
      this.render();
    });

    this.container.querySelector('#btn-force-submit')?.addEventListener('click', () => {
      if (!confirm('검증 없이 현재 상태로 제출합니다. 계속할까요?')) return;
      this.currentPage++;
      this.render();
    });

    this.bindThreadEvents();

    this.container.querySelectorAll('.option-list').forEach(list => {
      const qid = list.dataset.qid;
      const type = list.dataset.type;
      const q = this.findQuestion(qid);

      list.querySelectorAll('.option-item').forEach(item => {
        item.addEventListener('click', (e) => {
          if (e.target.classList.contains('other-text')) return;
          const input = item.querySelector('input[type="radio"], input[type="checkbox"]');
          if (!input || input.disabled) return;

          // <label class="option-item"> 내부에 input이 있어 브라우저가 이미 toggle 처리함.
          // 다음 프레임에서 최종 상태를 읽어 UI와 동기화 (수동 토글 금지).
          requestAnimationFrame(() => {
            if (type === 'radio') {
              list.querySelectorAll('.option-item').forEach(oi => oi.classList.remove('selected'));
              item.classList.add('selected');
              this.setResponse(qid, parseInt(input.value) || input.value);
            } else {
              item.classList.toggle('selected', input.checked);
              this.collectMultiResponse(qid, list, q);
            }

            if (q && q.exclusive !== undefined && input.checked) {
              const idx = parseInt(item.dataset.index);
              if (idx === q.exclusive) {
                list.querySelectorAll('.option-item').forEach(oi => {
                  if (oi !== item) {
                    const cb = oi.querySelector('input[type="checkbox"]');
                    if (cb) { cb.checked = false; oi.classList.remove('selected'); }
                  }
                });
              } else {
                const exItem = list.querySelector(`[data-index="${q.exclusive}"]`);
                if (exItem) {
                  const cb = exItem.querySelector('input[type="checkbox"]');
                  if (cb) { cb.checked = false; exItem.classList.remove('selected'); }
                }
              }
              this.collectMultiResponse(qid, list, q);
            }

            if (q && q.maxSelect) {
              this.enforceMaxSelect(qid, list, q);
            }

            const block = item.closest('.question-block, .sub-question');
            if (block) block.classList.remove('has-error');
          });
        });
      });
    });

    this.container.querySelectorAll('.likert-radio').forEach(radio => {
      radio.addEventListener('change', () => {
        const name = radio.name;
        const [qid, rowStr] = name.split(/_(\d+)$/);
        const row = parseInt(rowStr);
        const val = parseInt(radio.value);
        let resp = this.getResponse(qid) || {};

        // uniqueColumns: 같은 val을 다른 row가 이미 갖고 있으면 자동 해제 (한 순위 = 한 항목)
        const q = this.findQuestion(qid);
        if (q && Array.isArray(q.uniqueColumns) && q.uniqueColumns.includes(val)) {
          for (const otherRow of Object.keys(resp)) {
            if (parseInt(otherRow) !== row && resp[otherRow] === val) {
              delete resp[otherRow];
              const otherRadio = this.container.querySelector(
                `input[name="${qid}_${otherRow}"][value="${val}"]`
              );
              if (otherRadio) otherRadio.checked = false;
            }
          }
        }

        resp[row] = val;
        this.setResponse(qid, resp);

        const table = radio.closest('.likert-table');
        if (table) table.classList.remove('has-error');
      });
    });

    this.container.querySelectorAll('.text-input').forEach(el => {
      const qid = el.dataset.qid;
      el.addEventListener('input', () => {
        this.setResponse(qid, el.value);
        el.closest('.question-block')?.classList.remove('has-error');
      });
    });

    this.container.querySelectorAll('.other-text').forEach(el => {
      el.addEventListener('input', () => {
        const qid = el.dataset.qid;
        this.setResponse(qid, el.value);
      });
      el.addEventListener('click', (e) => e.stopPropagation());
    });

    this.container.querySelectorAll('.cell-input').forEach(input => {
      input.addEventListener('input', () => {
        const qid = input.dataset.q;
        const row = input.dataset.r;
        const col = input.dataset.c;
        let resp = this.getResponse(qid) || {};
        if (!resp[row] || resp[row] === 'unknown') resp[row] = {};
        const n = input.value === '' ? '' : Number(input.value);
        resp[row][col] = n;
        this.setResponse(qid, resp);
        this.updateNumberTableSum(qid, row);
        input.closest('.question-block')?.classList.remove('has-error');
      });
    });

    this.container.querySelectorAll('.row-unknown-toggle').forEach(cb => {
      cb.addEventListener('change', () => {
        const qid = cb.dataset.q;
        const row = cb.dataset.r;
        const tr = cb.closest('tr');
        const inputs = tr.querySelectorAll('.cell-input');
        let resp = this.getResponse(qid) || {};
        if (cb.checked) {
          inputs.forEach(inp => { inp.value = ''; inp.disabled = true; });
          resp[row] = 'unknown';
          const sumCell = this.container.querySelector(`.cell-sum[data-r="${row}"]`);
          if (sumCell) sumCell.textContent = '모름';
        } else {
          inputs.forEach(inp => { inp.disabled = false; });
          resp[row] = {};
          this.updateNumberTableSum(qid, row);
        }
        this.setResponse(qid, resp);
      });
    });

    this.container.querySelectorAll('.unknown-toggle').forEach(cb => {
      cb.addEventListener('change', () => {
        const qid = cb.dataset.target;
        const input = this.container.querySelector(`input.sub-text[data-qid="${qid}"]`);
        if (!input) return;
        if (cb.checked) {
          input.value = '';
          input.disabled = true;
          this.setResponse(qid, '(모름/해당없음)');
        } else {
          input.disabled = false;
          input.value = '';
          this.setResponse(qid, '');
          input.focus();
        }
        input.closest('.sub-question')?.classList.remove('has-error');
      });
    });
  }

  updateNumberTableSum(qid, row) {
    const resp = this.getResponse(qid);
    if (!resp || !resp[row] || resp[row] === 'unknown') return;
    const cells = resp[row];
    const sum = Object.values(cells).reduce((acc, v) => acc + (Number(v) || 0), 0);
    const sumCell = this.container.querySelector(`.cell-sum[data-r="${row}"]`);
    if (sumCell) sumCell.textContent = new Intl.NumberFormat('ko').format(sum);
  }

  collectMultiResponse(qid, list) {
    const checked = [];
    list.querySelectorAll('input:checked').forEach(cb => {
      checked.push(cb.value === 'other' ? 'other' : parseInt(cb.value));
    });
    this.setResponse(qid, checked);
  }

  enforceMaxSelect(qid, list, q) {
    const checked = list.querySelectorAll('input:checked');
    const unchecked = list.querySelectorAll('input:not(:checked)');
    if (checked.length >= q.maxSelect) {
      unchecked.forEach(cb => {
        cb.disabled = true;
        cb.closest('.option-item')?.classList.add('disabled');
      });
    } else {
      list.querySelectorAll('input').forEach(cb => {
        cb.disabled = false;
        cb.closest('.option-item')?.classList.remove('disabled');
      });
    }
  }

  // ── Restore Saved Values ──
  restoreValues(section) {
    const allQuestions = this.getAllQuestions(section);
    for (const q of allQuestions) {
      const val = this.getResponse(q.id);
      if (val === undefined) continue;

      if (q.type === Q_TYPE.LIKERT_TABLE) {
        if (typeof val === 'object') {
          for (const [row, v] of Object.entries(val)) {
            const radio = this.container.querySelector(`input[name="${q.id}_${row}"][value="${v}"]`);
            if (radio) radio.checked = true;
          }
        }
      } else if (q.type === Q_TYPE.NUMBER_TABLE) {
        if (typeof val === 'object') {
          for (const [row, rowVal] of Object.entries(val)) {
            if (rowVal === 'unknown') {
              const cb = this.container.querySelector(`.row-unknown-toggle[data-q="${q.id}"][data-r="${row}"]`);
              if (cb) {
                cb.checked = true;
                const tr = cb.closest('tr');
                tr.querySelectorAll('.cell-input').forEach(inp => { inp.disabled = true; });
                const sumCell = this.container.querySelector(`.cell-sum[data-r="${row}"]`);
                if (sumCell) sumCell.textContent = '모름';
              }
            } else if (typeof rowVal === 'object') {
              for (const [col, cellVal] of Object.entries(rowVal)) {
                const inp = this.container.querySelector(`.cell-input[data-q="${q.id}"][data-r="${row}"][data-c="${col}"]`);
                if (inp && cellVal !== '' && cellVal !== null && cellVal !== undefined) inp.value = cellVal;
              }
              this.updateNumberTableSum(q.id, row);
            }
          }
        }
      } else if (q.type === Q_TYPE.TEXT) {
        const el = this.container.querySelector(`input[data-qid="${q.id}"], textarea[data-qid="${q.id}"]`);
        if (el) {
          if (val === '(모름/해당없음)') {
            const toggle = this.container.querySelector(`.unknown-toggle[data-target="${q.id}"]`);
            if (toggle) {
              toggle.checked = true;
              el.disabled = true;
              el.value = '';
            } else {
              el.value = val;
            }
          } else {
            el.value = val;
          }
        }
      } else if (q.type === Q_TYPE.SINGLE || q.type === Q_TYPE.SINGLE_WITH_OTHER) {
        const list = this.container.querySelector(`.option-list[data-qid="${q.id}"]`);
        if (list) {
          const input = list.querySelector(`input[value="${val}"]`);
          if (input) {
            input.checked = true;
            input.closest('.option-item')?.classList.add('selected');
          }
        }
        if (val === 'other') {
          const otherText = this.getResponse(q.id + '_other');
          const otherInput = this.container.querySelector(`.other-text[data-qid="${q.id}_other"]`);
          if (otherInput && otherText) otherInput.value = otherText;
        }
      } else if (Array.isArray(val)) {
        const list = this.container.querySelector(`.option-list[data-qid="${q.id}"]`);
        if (list) {
          val.forEach(v => {
            const input = list.querySelector(`input[value="${v}"]`);
            if (input) {
              input.checked = true;
              input.closest('.option-item')?.classList.add('selected');
            }
          });
          if (q.maxSelect) this.enforceMaxSelect(q.id, list, q);
        }
        if (val.includes('other')) {
          const otherText = this.getResponse(q.id + '_other');
          const otherInput = this.container.querySelector(`.other-text[data-qid="${q.id}_other"]`);
          if (otherInput && otherText) otherInput.value = otherText;
        }
      }
    }
  }

  // ── Validation ──
  validateSection(section) {
    let valid = true;
    const allQuestions = this.getAllQuestions(section);

    for (const q of allQuestions) {
      if (q.optional) continue;

      const val = this.getResponse(q.id);
      let ok = true;

      if (q.type === Q_TYPE.LIKERT_TABLE) {
        const expected = q.items.length;
        ok = val && typeof val === 'object' && Object.keys(val).length === expected;
        if (!ok) {
          const table = this.container.querySelector(`.likert-table[data-qid="${q.id}"]`);
          table?.classList.add('has-error');
          this.showError(q.id, '모든 항목에 응답해 주십시오.');
        }
      } else if (q.type === Q_TYPE.NUMBER_TABLE) {
        ok = val && typeof val === 'object';
        if (ok) {
          for (const r of q.rows) {
            const rv = val[r.id];
            if (rv === 'unknown') continue;
            if (!rv || typeof rv !== 'object') { ok = false; break; }
            for (const c of q.columns) {
              const cv = rv[c.id];
              if (cv === undefined || cv === null || cv === '' || Number.isNaN(Number(cv))) {
                ok = false; break;
              }
            }
            if (!ok) break;
          }
        }
        if (!ok) {
          const wrap = this.container.querySelector(`.number-table-wrap[data-qid="${q.id}"]`);
          wrap?.classList.add('has-error');
          this.showError(q.id, '모든 연도·항목을 숫자로 입력하거나 "모름/해당없음"을 선택해 주십시오.');
        }
      } else if (q.type === Q_TYPE.TEXT) {
        ok = val && val.trim().length > 0;
        if (!ok) this.showError(q.id, '응답을 입력해 주십시오.');
        if (ok && q.pattern) {
          const re = new RegExp(q.pattern);
          if (!re.test(val.trim())) {
            ok = false;
            this.showError(q.id, q.patternMessage || '올바른 형식으로 입력해 주십시오.');
          }
        }
      } else if (q.type === Q_TYPE.SINGLE || q.type === Q_TYPE.SINGLE_WITH_OTHER) {
        ok = val !== undefined;
        if (!ok) this.showError(q.id, '하나를 선택해 주십시오.');
      } else if (Array.isArray(val)) {
        ok = val.length > 0;
        if (!ok) this.showError(q.id, '하나 이상 선택해 주십시오.');
      } else {
        ok = val !== undefined;
        if (!ok) this.showError(q.id, '응답해 주십시오.');
      }

      if (!ok) {
        valid = false;
        const block = this.container.querySelector(`[data-qid="${q.id}"]`);
        block?.classList.add('has-error');
      }
    }

    if (!valid) {
      const firstError = this.container.querySelector('.has-error');
      firstError?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    return valid;
  }

  showError(qid, msg) {
    const el = this.container.querySelector(`[data-error="${qid}"]`);
    if (el) el.textContent = msg;
  }

  // ── Helpers ──
  getAllQuestions(section) {
    const result = [];
    for (const q of section.questions) {
      if (this.shouldSkipQuestion(q)) continue;
      if (q.type === Q_TYPE.SUB_QUESTIONS) {
        for (const sq of q.subQuestions) result.push(sq);
      } else {
        result.push(q);
      }
    }
    return result;
  }

  findQuestion(qid) {
    for (const s of sections) {
      for (const q of s.questions) {
        if (q.id === qid) return q;
        if (q.type === Q_TYPE.SUB_QUESTIONS) {
          for (const sq of q.subQuestions) {
            if (sq.id === qid) return sq;
          }
        }
      }
    }
    return null;
  }

  escape(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  formatDateTime(isoStr) {
    try {
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return isoStr;
      const yyyy = d.getFullYear();
      const mm = String(d.getMonth() + 1).padStart(2, '0');
      const dd = String(d.getDate()).padStart(2, '0');
      const hh = String(d.getHours()).padStart(2, '0');
      const mi = String(d.getMinutes()).padStart(2, '0');
      return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
    } catch { return isoStr; }
  }

  // ── Completion ──
  renderCompletion() {
    if (this.token && (!this.submitted || this.editMode === EDIT_MODE.EDIT)) {
      this.submitToServer();
      return;
    }

    const statusBar = this.renderStatusBar();
    const alreadyMsg = this.submitted
      ? '<p class="resubmit-note">이전 응답이 업데이트되었습니다.</p>'
      : '';

    this.container.innerHTML = `
      ${statusBar}
      <div class="survey-container with-status-bar">
        <div class="completion">
          <div class="completion-icon">
            <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"></polyline></svg>
          </div>
          <h2>설문이 완료되었습니다</h2>
          <p>귀중한 시간을 내어 조사에 참여해 주셔서 진심으로 감사드립니다.<br/>
          수집된 결과는 「청사 관리에 관한 법률(가칭)」 제정 방향 및 청사 유지관리 정책 수립의 기초 자료로 활용됩니다.</p>
          ${alreadyMsg}
          <button class="btn btn-next" id="btn-download" style="margin-top:32px">응답 데이터 다운로드 (JSON)</button>
        </div>
      </div>
    `;
    this.container.querySelector('#btn-download')?.addEventListener('click', () => {
      this.downloadResponses();
    });
  }

  async submitToServer() {
    const statusBar = this.renderStatusBar();
    this.container.innerHTML = `
      ${statusBar}
      <div class="survey-container with-status-bar">
        <div class="completion" style="padding:120px 20px">
          <div class="spinner" style="width:40px;height:40px;border:3px solid #e0e0e0;border-top:3px solid #2c2c2c;border-radius:50%;animation:spin 0.8s linear infinite;margin:0 auto 24px"></div>
          <style>@keyframes spin{to{transform:rotate(360deg)}}</style>
          <h2>응답을 제출하고 있습니다…</h2>
        </div>
      </div>
    `;

    try {
      const payload = {
        token: this.token,
        survey_version: SURVEY_META.version,
        responses: { ...this.responses },
      };
      const res = await fetch(`${API_BASE}/api/responses`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) throw new Error(`Server error: ${res.status}`);
      const data = await res.json();

      this.submitted = true;
      const now = new Date().toISOString();
      if (data.status === 'created') this.submittedAt = now;
      else this.updatedAt = now;
      this.editMode = EDIT_MODE.NEW;
      this.renderCompletion();
    } catch (err) {
      const statusBar = this.renderStatusBar();
      this.container.innerHTML = `
        ${statusBar}
        <div class="survey-container with-status-bar">
          <div class="completion">
            <h2 style="color:var(--c-error)">제출 중 오류가 발생했습니다</h2>
            <p style="margin:16px 0">${err.message}<br/>응답은 브라우저에 저장되어 있습니다. 다시 시도하거나 JSON을 다운로드해 주십시오.</p>
            <button class="btn btn-next" id="btn-retry" style="margin:8px">다시 시도</button>
            <button class="btn btn-prev" id="btn-fallback" style="margin:8px">JSON 다운로드</button>
          </div>
        </div>
      `;
      this.container.querySelector('#btn-retry')?.addEventListener('click', () => this.submitToServer());
      this.container.querySelector('#btn-fallback')?.addEventListener('click', () => this.downloadResponses());
    }
  }

  downloadResponses() {
    const data = {
      meta: {
        survey: SURVEY_META.title,
        version: SURVEY_META.version,
        submittedAt: new Date().toISOString(),
        token: this.token || '',
      },
      responses: { ...this.responses },
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `survey_${data.meta.idCode || 'anon'}_${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }
}
