import { MantineProvider } from '@mantine/core';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ApiErrorAlert } from './Feedback';

const wrap = (ui: React.ReactElement) =>
  render(<MantineProvider>{ui}</MantineProvider>);

describe('ApiErrorAlert', () => {
  it('explains a 404 from the plugin API instead of crashing', () => {
    wrap(<ApiErrorAlert error={{ response: { status: 404 } }} />);
    expect(screen.getByText('MCP API not available')).toBeTruthy();
  });

  it('shows other errors as request failures', () => {
    wrap(
      <ApiErrorAlert
        error={{ response: { status: 500, data: { detail: 'boom' } } }}
      />
    );
    expect(screen.getByText('boom')).toBeTruthy();
  });
});
