<?php
/** Development declarations for the public Action Scheduler API shipped with WooCommerce. */
/** @param array<mixed> $args */
function as_enqueue_async_action(string $hook, array $args = [], string $group = '', bool $unique = false, int $priority = 10): int {}
/** @param array<mixed> $args */
function as_schedule_single_action(int $timestamp, string $hook, array $args = [], string $group = '', bool $unique = false, int $priority = 10): int {}
/** @param array<mixed> $args */
function as_schedule_recurring_action(int $timestamp, int $interval, string $hook, array $args = [], string $group = '', bool $unique = false, int $priority = 10): int {}
/** @param array<mixed>|null $args */
function as_has_scheduled_action(string $hook, ?array $args = null, string $group = ''): bool {}
/** @param array<mixed>|null $args */
function as_unschedule_all_actions(string $hook, ?array $args = null, string $group = ''): void {}
