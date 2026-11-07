ARG JENKINS_TAG=2.580.2-lts-jdk21
FROM jenkins/jenkins:${JENKINS_TAG}

COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt
