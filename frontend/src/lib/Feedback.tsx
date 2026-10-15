import { Alert, Button, Group, Modal, Text } from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { IconAlertCircle, IconPlugOff } from '@tabler/icons-react';
import { type ReactNode, useState } from 'react';
import { errorMessage, isNotWired } from './api';

/** Error display; a 404 from the plugin API means the MCP API is not reachable on this server. */
export function ApiErrorAlert({
  error,
  plugin = true
}: Readonly<{ error: unknown; plugin?: boolean }>) {
  if (plugin && isNotWired(error)) {
    return (
      <Alert
        color='yellow'
        icon={<IconPlugOff />}
        title='MCP API not available'
      >
        The MCP permission API is not available on this server. The plugin may
        be disabled, URL integration may be off, or the installed plugin version
        may be older than this page.
      </Alert>
    );
  }
  return (
    <Alert color='red' icon={<IconAlertCircle />} title='Request failed'>
      {errorMessage(error)}
    </Alert>
  );
}

export function notifyError(title: string, error: unknown) {
  notifications.show({ title, message: errorMessage(error), color: 'red' });
}

export function notifyOk(title: string, message: string) {
  notifications.show({ title, message, color: 'green' });
}

/** A button that asks for confirmation in a modal before running `onConfirm`. */
export function ConfirmButton({
  label,
  message,
  confirmLabel,
  onConfirm,
  color = 'red',
  variant = 'filled',
  size,
  leftSection,
  disabled
}: Readonly<{
  label: ReactNode;
  message: ReactNode;
  confirmLabel?: string;
  onConfirm: () => void | Promise<void>;
  color?: string;
  variant?: string;
  size?: string;
  leftSection?: ReactNode;
  disabled?: boolean;
}>) {
  const [opened, setOpened] = useState(false);
  const [busy, setBusy] = useState(false);

  const confirm = async () => {
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
      setOpened(false);
    }
  };

  return (
    <>
      <Modal
        opened={opened}
        onClose={() => setOpened(false)}
        title='Please confirm'
        centered
      >
        <Text mb='md'>{message}</Text>
        <Group justify='right'>
          <Button variant='default' onClick={() => setOpened(false)}>
            Cancel
          </Button>
          <Button color={color} loading={busy} onClick={confirm}>
            {confirmLabel ?? 'Confirm'}
          </Button>
        </Group>
      </Modal>
      <Button
        color={color}
        variant={variant}
        size={size}
        leftSection={leftSection}
        disabled={disabled}
        onClick={() => setOpened(true)}
      >
        {label}
      </Button>
    </>
  );
}
