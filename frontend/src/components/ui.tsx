import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { AlertCircle, Check, FileSearch, LoaderCircle, X } from "lucide-react";

export function Button({
  children,
  variant = "secondary",
  busy,
  className = "",
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  busy?: boolean;
}) {
  return (
    <button
      {...props}
      disabled={disabled || busy}
      className={`button button-${variant} ${className}`}
      aria-busy={busy || undefined}
    >
      {busy && <LoaderCircle size={15} className="spin" />}
      {children}
    </button>
  );
}
export function ErrorNotice({
  error,
  retry,
}: {
  error: Error | null;
  retry?: () => unknown;
}) {
  if (!error) return null;
  return (
    <div role="alert" className="notice notice-error">
      <AlertCircle size={17} />
      <span>{error.message}</span>
      {retry && (
        <Button variant="ghost" onClick={() => void retry()}>
          重试
        </Button>
      )}
    </div>
  );
}
export function Loading({ label = "正在读取本地状态…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle size={20} className="spin" />
      <span>{label}</span>
    </div>
  );
}
export function EmptyState({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <FileSearch size={48} strokeWidth={1.2} aria-hidden="true" />
      <h3>{title}</h3>
      <p>{description}</p>
      {children}
    </div>
  );
}
export function Modal({
  open,
  onOpenChange,
  title,
  description,
  children,
  drawer = false,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  title: string;
  description: string;
  children: ReactNode;
  drawer?: boolean;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="modal-overlay" />
        <Dialog.Content className={drawer ? "modal drawer" : "modal"}>
          <header className="modal-header">
            <div>
              <Dialog.Title>{title}</Dialog.Title>
              <Dialog.Description>{description}</Dialog.Description>
            </div>
            <Dialog.Close asChild>
              <Button variant="ghost" aria-label="关闭">
                <X size={20} />
              </Button>
            </Dialog.Close>
          </header>
          <div className="modal-body">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
type Notice = { text: string; error?: boolean };
const NoticeContext = createContext<(notice: Notice) => void>(() => {});
export const useNotice = () => useContext(NoticeContext);
export function NoticeProvider({ children }: { children: ReactNode }) {
  const [notice, setNotice] = useState<Notice | null>(null);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(
      () => setNotice(null),
      notice.error ? 10_000 : 6_000,
    );
    return () => window.clearTimeout(timer);
  }, [notice]);
  return (
    <NoticeContext.Provider value={setNotice}>
      {children}
      {notice && (
        <div
          className={`toast ${notice.error ? "toast-error" : ""}`}
          role={notice.error ? "alert" : "status"}
        >
          {notice.error ? <AlertCircle size={18} /> : <Check size={18} />}
          <span>{notice.text}</span>
          <button aria-label="关闭提示" onClick={() => setNotice(null)}>
            <X size={16} />
          </button>
        </div>
      )}
    </NoticeContext.Provider>
  );
}
