import assert from "node:assert/strict";
import test from "node:test";

import { NoticeGate } from "./notice_gate.mjs";

const NOTICE = {
  task_id: "task-1",
  label: "revisar o laudo",
  summary: "Laudo revisado.",
  status: "completed",
};

test("pedido do dono durante aviso pendente é atendido primeiro", () => {
  const gate = new NoticeGate();
  gate.notice(NOTICE);
  // O SDK ainda vai responder a notificação: a janela está aberta.
  assert.equal(gate.waiting(), true);

  const owner = gate.ownerAsk();

  // A sessão cede para o dono em vez de ficar ocupada com o relatório.
  assert.equal(owner.preempt, true);
  assert.equal(owner.pending, 1);
  assert.equal(gate.waiting(), false);
});

test("o aviso é entregue depois, sozinho, e não se perde", () => {
  const gate = new NoticeGate();
  gate.notice(NOTICE);
  gate.ownerAsk();

  // Terminou o turno do dono: a sessão está livre e o anúncio sai.
  const batch = gate.takeRequest();
  assert.ok(batch);
  assert.deepEqual(batch.items.map((item) => item.task_id), ["task-1"]);
  // Só uma vez: pedir de novo não duplica o anúncio pendente.
  assert.equal(gate.takeRequest(), null);

  // Relatório anunciado: o mesmo aviso não volta a ser anunciado.
  gate.start();
  gate.say("Terminei em segundo plano: revisar o laudo.");
  const closed = gate.end({ complete: true });
  assert.equal(closed.announced, 1);
  assert.equal(gate.pending(), false);
  assert.equal(gate.notice(NOTICE), "announced");
  assert.equal(gate.ownerAsk().preempt, false);
});

test("anúncio cortado pelo dono continua pendente e não repete o que já foi dito", () => {
  const gate = new NoticeGate();
  gate.notice(NOTICE);
  gate.start();
  gate.say("Comecei a revisão.");

  gate.ownerAsk();
  // O turno de anúncio foi abortado, não concluído.
  const cut = gate.end({ complete: false });
  assert.equal(cut.announced, 0);
  assert.equal(gate.pending(), true);

  const batch = gate.takeRequest();
  assert.ok(batch);
  assert.deepEqual(batch.already, ["Comecei a revisão."]);
});

test("dois avisos não viram fala dupla", () => {
  const gate = new NoticeGate();
  const other = { task_id: "task-2", label: "testes", status: "completed" };

  // Os dois chegam enquanto o mesmo relatório está para sair: uma leva só.
  assert.equal(gate.notice(NOTICE), "queued");
  assert.equal(gate.notice(other), "batched");

  const batch = gate.takeRequest();
  assert.equal(batch, null, "sem corte nem teto não há reanúncio");

  gate.start();
  gate.say("As duas tarefas terminaram.");
  assert.equal(gate.end({ complete: true }).announced, 2);

  // A mesma notificação chegando de novo não vira segunda fala.
  assert.equal(gate.notice({ ...NOTICE }), "announced");
  assert.equal(gate.notice({ ...NOTICE, status: "failed" }), "queued");
});

test("teto de segurança devolve o aviso para a fila em vez de descartar", () => {
  const gate = new NoticeGate();
  gate.notice(NOTICE);
  gate.expire();
  const batch = gate.takeRequest();
  assert.ok(batch);
  assert.equal(batch.items.length, 1);
});
