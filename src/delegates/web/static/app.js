// Minimal progressive-enhancement layer: every study form posts to the server
// and swaps the returned fragment into #inner. The server re-derives the stage
// each time, so the client holds no state that could drift.
(function () {
  const inner = () => document.getElementById("inner");
  let renderedAt = Date.now();

  function markRendered() {
    renderedAt = Date.now();
    const el = document.querySelector("[data-autofocus]");
    if (el) el.focus();
  }

  async function post(url, formData) {
    formData.set("ms_elapsed", String(Date.now() - renderedAt));
    const busy = document.getElementById("stage");
    if (busy) busy.classList.add("busy");
    const res = await fetch(url, { method: "POST", body: formData });
    const html = await res.text();
    inner().innerHTML = html;
    markRendered();
    wire();
    autoAdvance();
  }

  function wire() {
    document.querySelectorAll("form[data-swap]").forEach((form) => {
      if (form.dataset.wired) return;
      form.dataset.wired = "1";
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        post(form.action, new FormData(form));
      });
    });

    // Two-tap rating: position, then confidence submits.
    const rating = document.getElementById("rating-form");
    if (rating && !rating.dataset.tapWired) {
      rating.dataset.tapWired = "1";
      const posInput = rating.querySelector("input[name=position]");
      rating.querySelectorAll("[data-pos]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          rating.querySelectorAll("[data-pos]").forEach((b) =>
            b.classList.remove("on")
          );
          btn.classList.add("on");
          posInput.value = btn.dataset.pos;
          rating.querySelector(".conf-row").classList.add("live");
        });
      });
      rating.querySelectorAll("[data-conf]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          if (posInput.value === "") return;
          rating.querySelector("input[name=confidence]").value = btn.dataset.conf;
          post(rating.action, new FormData(rating));
        });
      });
    }

    // Verdict: pick an option (selects, enables Submit), optionally comment,
    // then Submit posts. The form has data-swap so submit goes through post().
    const verdict = document.getElementById("verdict-form");
    if (verdict && !verdict.dataset.verdictWired) {
      verdict.dataset.verdictWired = "1";
      const submit = verdict.querySelector("button[type=submit]");
      verdict.querySelectorAll("[data-verdict]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.preventDefault();
          verdict.querySelectorAll("[data-verdict]").forEach((b) => b.classList.remove("on"));
          btn.classList.add("on");
          verdict.querySelector("input[name=verdict]").value = btn.dataset.verdict;
          submit.disabled = false;
        });
      });
      verdict.addEventListener("submit", (e) => {
        if (verdict.querySelector("input[name=verdict]").value === "") e.stopImmediatePropagation();
      }, true);
    }

    const box = document.querySelector("textarea[name=content]");
    if (box && !box.dataset.wired) {
      box.dataset.wired = "1";
      const counter = document.getElementById("charcount");
      const update = () => {
        if (counter) counter.textContent = box.value.trim().length + " characters";
      };
      box.addEventListener("input", update);
      update();
    }
  }

  function autoAdvance() {
    const el = document.querySelector("[data-advance]");
    if (el) post(el.dataset.advance, new FormData());
  }

  document.addEventListener("DOMContentLoaded", () => {
    markRendered();
    wire();
    autoAdvance();
  });
})();
