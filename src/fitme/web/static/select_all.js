// "Select all" checkbox for list tables. Served from 'self' (the CSP forbids inline script).
// The header checkbox has data-select-all="<name of the row checkboxes>" and starts hidden,
// so without JavaScript nothing non-functional is shown.

for (const master of document.querySelectorAll("input[type=checkbox][data-select-all]")) {
  const form = master.closest("form");
  if (!form) continue;
  const rows = () =>
    form.querySelectorAll(`input[type=checkbox][name="${master.dataset.selectAll}"]`);

  const sync = () => {
    const boxes = [...rows()];
    const checked = boxes.filter((box) => box.checked).length;
    master.checked = boxes.length > 0 && checked === boxes.length;
    master.indeterminate = checked > 0 && checked < boxes.length;
  };

  master.addEventListener("change", () => {
    for (const box of rows()) box.checked = master.checked;
    master.indeterminate = false;
  });
  form.addEventListener("change", (event) => {
    if (event.target !== master) sync();
  });

  master.hidden = false;
  sync();
}
