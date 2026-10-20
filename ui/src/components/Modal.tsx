import { ReactNode, useEffect, useRef } from "react";
import { useI18n } from "../i18n";
import { I } from "./icons";

/** A centred dialog over a dimmed page. Esc or a click outside closes it; focus moves inside and comes back. */
export default function Modal({ title, onClose, children, footer, size = "md", busy = false }: {
  title: ReactNode; onClose: () => void; children?: ReactNode; footer?: ReactNode; size?: "sm" | "md"; busy?: boolean;
}) {
  const { tr } = useI18n();
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    // Fields first, then the main action; never the destructive one, so Enter can't delete by accident.
    const first = box.current?.querySelector<HTMLElement>("[autofocus], input, select, textarea")
      ?? box.current?.querySelector<HTMLElement>(".modal-foot .btn.primary")
      ?? box.current?.querySelector<HTMLElement>(".modal-foot .btn:not(.danger)");
    first?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      before?.focus?.();
    };
  }, []);
  return (
    <div className="modal-wrap" onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div ref={box} className={`modal ${size}`} role="dialog" aria-modal="true" aria-label={typeof title === "string" ? title : undefined}>
        <div className="modal-head"><h2>{title}</h2>
          <button className="btn ghost sm icon-only" aria-label={tr("Close", "إغلاق")} onClick={onClose} disabled={busy}><I.close size={16} /></button></div>
        {children && <div className="modal-body">{children}</div>}
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}
