import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Toast from './Toast.jsx';

afterEach(() => vi.useRealTimers());

describe('Toast', () => {
  it('closes by itself after 3 s even when the parent re-renders with a new onClose', () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    const { rerender } = render(<Toast message="Added to cart" type="success" onClose={() => onClose()} />);
    act(() => { vi.advanceTimersByTime(2000); });
    rerender(<Toast message="Added to cart" type="success" onClose={() => onClose()} />);
    act(() => { vi.advanceTimersByTime(900); });
    expect(onClose).not.toHaveBeenCalled();
    act(() => { vi.advanceTimersByTime(200); });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not fire after unmount and still closes early with the x', () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    const { unmount } = render(<Toast message="Hi" onClose={onClose} />);
    screen.getByLabelText('Dismiss').click();
    expect(onClose).toHaveBeenCalledTimes(1);
    unmount();
    act(() => { vi.advanceTimersByTime(5000); });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
