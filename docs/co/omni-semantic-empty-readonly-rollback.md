This is a read-only investigation and does not change production state.

Rollback plan:

1. No service rollback is required because no mutation is planned.
2. If any command unexpectedly creates a temporary local diagnostic file on the production host, remove only that exact temporary file before closure.
3. Close the change as `Successfully Closed` when investigation completes, or `Not Executed` if no production access occurs.
