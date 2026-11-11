# Fetches a migration tool from an internal artifact server that this build cannot resolve.
# The pipe hides wget's failure: the step fails on tar, which got no input.
FROM busybox:1.36
RUN mkdir -p /opt/tools && wget -qO- http://artifacts.pfa-lab.invalid/tools/migrate-2.4.1.tar.gz | tar xz -C /opt/tools
CMD ["/opt/tools/migrate", "--help"]
