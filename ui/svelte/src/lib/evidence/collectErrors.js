function errorMessage(reason) {
  if (reason instanceof Error && reason.message) {
    return reason.message;
  }
  return String(reason);
}

export async function collectAll(tasks, options = {}) {
  const settled = await Promise.allSettled(tasks.map((task) => task.promise));
  const values = {};
  const errors = [];

  settled.forEach((result, index) => {
    const task = tasks[index];
    if (result.status === 'fulfilled') {
      values[task.key] = result.value;
      return;
    }

    errors.push({
      key: task.key,
      error: errorMessage(result.reason),
    });
  });

  if (options.throwOnAny && errors.length > 0) {
    throw new AggregateError(
      errors.map((entry) => new Error(`${entry.key}: ${entry.error}`)),
      'collectAll failed',
    );
  }

  return { values, errors };
}
