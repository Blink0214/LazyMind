import { useContext, useEffect, useRef, useState, type FocusEvent } from 'react';
import { Input, message, Modal } from 'antd';
import { useTranslation } from 'react-i18next';
import { WorkflowSessionApi, type WriterCopyFormat } from '@/modules/chat/utils/request';
import { SlotEditingContext, WorkflowPanelTabActiveContext } from './slotEditingContext';
import { isWriterDocument, normalizeWriterDocumentForSync } from './writerIR';

const FORMATS: WriterCopyFormat[] = ['markdown', 'latex', 'text'];
const STORAGE_KEY = 'writer-copy-format';

function preferredFormat(): WriterCopyFormat {
  try {
    const value = localStorage.getItem(STORAGE_KEY) as WriterCopyFormat;
    return FORMATS.includes(value) ? value : 'markdown';
  } catch { return 'markdown'; }
}

export function useDocumentCopy({
  enabled, editingKey, sessionId, slotId, listIndex = -1, revision, document, sourceKey,
}: {
  sourceKey?: string;
  enabled: boolean;
  editingKey?: string;
  sessionId?: string;
  slotId?: string;
  listIndex?: number;
  revision?: number;
  document: unknown;
}) {
  const { t } = useTranslation();
  const { registerFooterAction, getSnapshot } = useContext(SlotEditingContext);
  const tabActive = useContext(WorkflowPanelTabActiveContext);
  const [format, setFormat] = useState(preferredFormat);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const current = useRef({ document, revision, getSnapshot });
  current.current = { document, revision, getSnapshot };

  useEffect(() => {
    if (!enabled || !tabActive || !editingKey || !sessionId || !slotId) return undefined;
    const copy = async (selected: WriterCopyFormat) => {
      if (pending.current) return;
      const snapshot = current.current.getSnapshot?.(editingKey) ?? current.current.document;
      const baseRevision = current.current.revision;
      if (!baseRevision || snapshot === null || snapshot === undefined) return;
      pending.current = true;
      setBusy(true);
      setFormat(selected);
      try { localStorage.setItem(STORAGE_KEY, selected); } catch { /* Optional preference. */ }
      try {
        const response = await WorkflowSessionApi().convertDocument(
          sessionId, slotId, listIndex, baseRevision, selected,
          isWriterDocument(snapshot) ? normalizeWriterDocumentForSync(snapshot) : snapshot,
        );
        const result = response.data.data;
        if (response.data.code !== 0 || result?.format !== selected || typeof result.content !== 'string') {
          throw new Error('Invalid conversion response');
        }
        try {
          await navigator.clipboard.writeText(result.content);
          message.success(t('chat.writerCopy.success'));
        } catch {
          Modal.confirm({
            title: t('chat.writerCopy.manualTitle'),
            content: <Input.TextArea value={result.content} readOnly autoSize={{ minRows: 5, maxRows: 12 }}
              onFocus={(event: FocusEvent<HTMLTextAreaElement>) => event.target.select()} />,
            okText: t('chat.writerCopy.retry'),
            cancelText: t('common.cancel'),
            onOk: async () => {
              await navigator.clipboard.writeText(result.content);
              message.success(t('chat.writerCopy.success'));
            },
          });
        }
      } catch {
        message.error(t('chat.writerCopy.failed'));
      } finally {
        pending.current = false;
        setBusy(false);
      }
    };
    return registerFooterAction(`${editingKey}:copy`, {
      label: busy ? t('chat.writerCopy.converting') : t('chat.writerCopy.copyAs', { format: t(`chat.writerCopy.${format}`) }),
      dedupKey: sourceKey ? JSON.stringify(['copy', sessionId, sourceKey]) : undefined,
      icon: 'copy',
      order: 20,
      disabled: busy,
      onClick: () => { void copy(format); },
      menu: FORMATS.map((value) => ({
        key: value,
        label: t('chat.writerCopy.copyAs', { format: t(`chat.writerCopy.${value}`) }),
        onClick: () => { void copy(value); },
      })),
    });
  }, [busy, editingKey, enabled, format, listIndex, registerFooterAction, sessionId, slotId, sourceKey, t, tabActive]);
}
