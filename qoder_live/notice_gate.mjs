/**
 * Prioridade DONO > relatório de segundo plano.
 *
 * Laudo de 02/11/2026 (20:15–21:06): existia uma única sessão Qoder viva e os
 * pedidos do dono entravam na fila atrás do relatório de conclusão de uma
 * `task_notification`. A primeira frase do turno do dono saía 28–59 s depois
 * ("[Latência] texto → 1ª frase"), repetidas vezes. A regra do dono é dura:
 * ele nunca é ignorado nem cortado, e toda fala dele exige resposta.
 *
 * Aqui ficam só as decisões da política. O relatório que cede a vez não se
 * perde: continua pendente, é anunciado em voz alta (proativo, sem o dono
 * pedir) quando a sessão fica livre para ele, e o mesmo aviso nunca vira fala
 * repetida.
 *
 * O módulo é puro — sem timers, sem I/O, sem SDK — para o comportamento ser
 * provado fora da sessão Qoder. O sidecar (index.mjs) apenas traduz os eventos
 * do SDK para estas chamadas.
 */
export class NoticeGate {
  constructor() {
    // Turno de anúncio em curso (sem id de ask): o modelo está escrevendo o
    // relatório de uma notificação.
    this.inflight = false;
    // O SDK deve responder uma notificação que ainda não começou a render
    // texto; a janela existe para o relatório não ser atropelado nem trocado
    // de canal (id do ask carimbado no result do relatório).
    this.expecting = false;
    // Notificações ainda NÃO anunciadas ao dono (nunca descartadas).
    this.batch = [];
    // Frases do anúncio em curso que já saíram: o reanúncio não as repete.
    this.already = [];
    // O anúncio foi adiado e precisa ser pedido de novo quando a vez voltar.
    this.needsRequest = false;
    // Chaves de notificações já anunciadas: o mesmo aviso não vira fala dupla.
    this.announced = new Set();
    this.yields = 0;
    this.requests = 0;
  }

  /** Identidade do aviso: a mesma tarefa no mesmo estado é o mesmo aviso. */
  static key(item) {
    const id = String(item?.task_id ?? item?.id ?? item?.label ?? "").trim();
    const status = String(item?.status ?? "").trim();
    return `${id}:${status}`;
  }

  /** Há um anúncio esperando ou em curso (o dono, se chegar, passa na frente). */
  waiting() {
    return this.expecting || this.inflight;
  }

  /** Há notificação que ainda não foi anunciada. */
  pending() {
    return this.batch.length > 0;
  }

  /**
   * Uma `task_notification` chegou.
   *
   * - "announced": este aviso já foi dito ao dono; não se anuncia de novo.
   * - "duplicate": já está na leva atual; não vira segunda fala.
   * - "batched": o modelo já está escrevendo o relatório desta mesma sessão,
   *   então a notificação entra nele em vez de virar um segundo relatório.
   * - "queued": pendente, o SDK ainda vai responder a notificação.
   */
  notice(item) {
    const key = NoticeGate.key(item);
    if (this.announced.has(key)) return "announced";
    if (this.batch.some((entry) => entry.key === key)) return "duplicate";
    this.batch.push({ ...(item || {}), key });
    if (this.waiting()) return "batched";
    // O SDK vai responder a notificação sem id de ask: a janela fica aberta.
    this.expecting = true;
    return "queued";
  }

  /** Começou a sair o texto do anúncio (primeira mensagem sem id de ask). */
  start() {
    if (this.inflight || !this.batch.length) return false;
    this.inflight = true;
    this.expecting = false;
    return true;
  }

  /** Frase do anúncio já entregue ao dono (para não repetir no reanúncio). */
  say(text) {
    const clean = String(text || "").trim();
    if (clean) this.already.push(clean);
  }

  /**
   * O turno de anúncio terminou.
   *
   * `complete: true` — o relatório foi escrito inteiro: o aviso está
   * anunciado para sempre. `complete: false` — foi cortado (abort para
   * atender o dono ou teto estourado): continua pendente e será pedido de
   * novo, sem perder nada.
   */
  end({ complete = false } = {}) {
    const spoke = this.already.length > 0;
    let announced = 0;
    if (complete && this.batch.length) {
      for (const entry of this.batch) this.announced.add(entry.key);
      announced = this.batch.length;
      this.batch = [];
      this.needsRequest = false;
      this.already = [];
    } else if (this.batch.length) {
      this.needsRequest = true;
    }
    this.inflight = false;
    this.expecting = false;
    if (complete) this.already = [];
    return { announced, spoke };
  }

  /**
   * Chegou um pedido do dono (voz no microfone ou chat direto no WhatsApp).
   *
   * Devolve `preempt: true` quando o relatório está esperando/ocupando a
   * sessão: o sidecar aborta o turno de anúncio e atende o dono. O relatório
   * fica pendente para o próximo momento livre.
   */
  ownerAsk() {
    if (!this.waiting()) return { preempt: false, pending: 0, already: [] };
    const pending = this.batch.length;
    this.inflight = false;
    this.expecting = false;
    if (pending) this.needsRequest = true;
    this.yields += 1;
    return { preempt: true, pending, already: this.already.slice() };
  }

  /**
   * A sessão está livre (e o dono não tem nada na fila): devolve a leva de
   * notificações que precisa ser anunciada agora, ou null. Quem chama monta a
   * mensagem interna de reanúncio e marca o turno como em curso.
   */
  takeRequest() {
    if (this.inflight || !this.needsRequest) return null;
    this.needsRequest = false;
    if (!this.batch.length) return null;
    this.inflight = true;
    this.requests += 1;
    return { items: this.batch.slice(), already: this.already.slice() };
  }

  /** O teto de segurança estourou sem relatório: pedir explicitamente depois. */
  expire() {
    this.inflight = false;
    this.expecting = false;
    if (this.batch.length) this.needsRequest = true;
  }
}
